"""Rule-based trade matcher (PLAN.md §6, RESEARCH §5.5). No LLM in v1.

Algorithm (PLAN §6.3):
1. text = project name + description + location (never the whole agenda item);
2. code-prefix prior (``RPR3542`` -> ROAD_RESURF) with evidence ``prefix:RPR``;
3. per trade, positive regexes; a negative regex within 60 chars turns the hit into ``on_negative``
   (default NON_CIVIL); bare ambiguous tokens (pipe, path, landscap) need context words;
4. composite rules add implied labels (``method=rule_implied``, evidence ``implied:ROAD_CONST``);
5. no label -> UNKNOWN; any civil label drops NON_CIVIL/OTHER_CIVIL unless it came from a negative override.
"""

from __future__ import annotations

import csv
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from radar.settings import TradesConfig, load_trades

NEGATIVE_WINDOW = 60
NON_CIVIL_CODES = {"NON_CIVIL", "OTHER_CIVIL", "UNKNOWN"}
_CODE_PREFIX = re.compile(r"^([A-Za-z]{2,4})\d{2,}")


@dataclass(frozen=True)
class Label:
    code: str
    method: str          # rule | rule_implied | llm (reserved)
    evidence: str        # kw:<pattern> | prefix:<code> | implied:<from> | neg:<pattern>|kw:<pattern> | no_rule

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "method": self.method, "evidence": self.evidence}


def classify_fallback(text: str) -> list[Label]:
    """LLM hook (PLAN §6.4): returns [] in v1. A later local model may suggest labels for UNKNOWN items only."""
    return []


def _gap(a: tuple[int, int], b: tuple[int, int]) -> int:
    return max(0, b[0] - a[1], a[0] - b[1])


class TradeMatcher:
    def __init__(self, config: TradesConfig | None = None) -> None:
        self.config = config or load_trades()
        flags = re.IGNORECASE
        self._pos = {c: [(p, re.compile(p, flags)) for p in t.keywords] for c, t in self.config.trades.items()}
        self._neg = {c: [(p, re.compile(p, flags)) for p in t.negative] for c, t in self.config.trades.items()}
        self._order = list(self.config.trades)

    # -- helpers --
    def _context_ok(self, matched: str, text_lower: str) -> bool:
        token = matched.strip().lower()
        if " " in token:
            return True
        for key, words in self.config.context_required.items():
            if token.startswith(key):
                return any(w.lower() in text_lower for w in words)
        return True

    def _match_trade(self, code: str, text: str, text_lower: str) -> Label | None:
        trade = self.config.trades[code]
        negatives = [(p, m.span()) for p, rx in self._neg[code] for m in rx.finditer(text)]
        first_override: Label | None = None
        for pattern, rx in self._pos[code]:
            for m in rx.finditer(text):
                if not self._context_ok(m.group(0), text_lower):
                    continue
                near = next((p for p, span in negatives if _gap(m.span(), span) <= NEGATIVE_WINDOW), None)
                if near is None:
                    return Label(code, "rule", f"kw:{pattern}")
                if first_override is None:
                    target = trade.on_negative or "NON_CIVIL"
                    first_override = Label(target, "rule", f"neg:{near}|kw:{pattern}")
        return first_override

    # -- public --
    def match(self, name: str | None, description: str | None = None, location: str | None = None, *,
              council: str | None = None, project_code: str | None = None) -> list[Label]:
        text = " ".join(p for p in (name, description, location) if p)
        text_lower = text.lower()
        found: dict[str, Label] = {}
        overrides: set[str] = set()

        # 2. code-prefix prior
        if council and project_code:
            m = _CODE_PREFIX.match(project_code.strip())
            if m:
                prefix = m.group(1).upper()
                target = (self.config.code_prefixes.get(council) or {}).get(prefix)
                if target:
                    found[target] = Label(target, "rule", f"prefix:{prefix}")

        # 3. keyword rules
        for code in self._order:
            if code in ("UNKNOWN", "OTHER_CIVIL") or not self.config.trades[code].keywords:
                continue
            label = self._match_trade(code, text, text_lower)
            if label is None:
                continue
            if label.evidence.startswith("neg:"):
                overrides.add(label.code)
            found.setdefault(label.code, label)

        # 4. composite / implied labels
        for rule in self.config.composite_rules:
            when = rule["when"]
            src = found.get(when)
            if src is None or when in overrides:
                continue
            rx = rule.get("evidence_regex")
            if rx and not re.search(rx, src.evidence):
                continue
            for implied in rule.get("implies", []):
                found.setdefault(implied, Label(implied, rule.get("method", "rule_implied"), f"implied:{when}"))

        # 5. drop residual labels when civil work is present (negative overrides stay)
        if any(c not in NON_CIVIL_CODES for c in found):
            for c in list(found):
                if c in ("NON_CIVIL", "OTHER_CIVIL") and c not in overrides:
                    del found[c]
        if not found:
            return [Label("UNKNOWN", "rule", "no_rule")]
        order = {c: i for i, c in enumerate(self._order)}
        return sorted(found.values(), key=lambda lab: order.get(lab.code, 99))


@lru_cache(maxsize=1)
def default_matcher() -> TradeMatcher:
    return TradeMatcher()


def match_trades(name: str | None, description: str | None = None, location: str | None = None, *,
                 council: str | None = None, project_code: str | None = None) -> list[dict[str, str]]:
    """Convenience wrapper returning ``trade_labels`` dicts for ``ProjectMention``."""
    return [lab.to_dict() for lab in default_matcher().match(name, description, location, council=council,
                                                             project_code=project_code)]


def label_mention(mention: Any, description: str | None = None) -> list[dict[str, str]]:
    """Compute trade labels for a ``ProjectMention`` (name + description + location only)."""
    return match_trades(mention.project_name_raw, description, mention.location_raw, council=mention.council_id,
                        project_code=mention.council_project_code)


# ---- gold-set evaluation (PLAN §6.5) -----------------------------------------------------------
GOLD_PATH = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "trade_gold.csv"


def load_gold(path: Path = GOLD_PATH) -> list[dict[str, Any]]:
    rows = []
    with Path(path).open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(r for r in fh if not r.startswith("#")):
            rows.append({**row, "labels": {x for x in row["labels"].split("|") if x}})
    return rows


def evaluate_gold(rows: list[dict[str, Any]], matcher: TradeMatcher | None = None) -> dict[str, dict[str, float]]:
    """Per-label precision/recall over the gold rows. Only labels present in gold appear in the result."""
    matcher = matcher or default_matcher()
    tp, fp, fn = Counter(), Counter(), Counter()
    for row in rows:
        got = {lab.code for lab in matcher.match(row["name"], row.get("description"), None,
                                                 council=row.get("council") or None,
                                                 project_code=row.get("code") or None)}
        want = row["labels"]
        for c in got & want:
            tp[c] += 1
        for c in got - want:
            fp[c] += 1
        for c in want - got:
            fn[c] += 1
    out: dict[str, dict[str, float]] = {}
    for c in sorted({c for r in rows for c in r["labels"]}):
        p = tp[c] / (tp[c] + fp[c]) if tp[c] + fp[c] else 0.0
        r = tp[c] / (tp[c] + fn[c]) if tp[c] + fn[c] else 0.0
        out[c] = {"precision": round(p, 3), "recall": round(r, 3), "tp": tp[c], "fp": fp[c], "fn": fn[c]}
    return out
