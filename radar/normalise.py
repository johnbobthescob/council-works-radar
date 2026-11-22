"""Normaliser (PLAN.md §2.1 [5], §5.2 "Normalisation").

Pure functions: money, GST basis, financial years, road names / abbreviations / direction qualifiers,
road segments ("Elderberry Dr – South Lake to Berrigan Dr"), intersection pairs and a simple suburb
lookup against ``config/suburbs/<council>.txt``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from radar.settings import CONFIG_DIR

SUBURBS_DIR = CONFIG_DIR / "suburbs"

# ---- money -------------------------------------------------------------------------------------
_MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mil": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9}
_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_MULT_RE = r"(?:\s*(?:million|billion|thousand|mil|bn|[kmb])\b)?"
_MONEY_FIND = re.compile(
    rf"(?P<neg>[-−–]\s*)?(?P<open>\()?\s*\$\s*(?P<num>{_NUM})(?P<mult>{_MULT_RE})\s*(?P<close>\))?", re.IGNORECASE
)
_DASH_ONLY = re.compile(r"^\s*\$?\s*[-–—−]\s*$")
_PLAIN = re.compile(rf"^\s*(?P<open>\()?\s*\$?\s*(?P<neg>[-−])?\s*(?P<num>{_NUM})(?P<mult>{_MULT_RE})\s*(?P<close>\))?\s*$", re.IGNORECASE)


def _to_amount(num: str, mult: str | None, negative: bool) -> float:
    value = float(num.replace(",", ""))
    if mult and mult.strip():
        value *= _MULT[mult.strip().lower()]
    return -value if negative else value


def parse_money(text: str | None) -> float | None:
    """Parse one money cell/phrase. ``$4.3 million`` -> 4300000, ``(1,234)`` -> -1234, ``-`` -> 0, junk -> None."""
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    if _DASH_ONLY.match(s):
        return 0.0
    m = _PLAIN.match(s)
    if not m:
        return None
    if bool(m["open"]) != bool(m["close"]):
        return None
    return _to_amount(m["num"], m["mult"], negative=bool(m["open"]) or bool(m["neg"]))


@dataclass(frozen=True)
class MoneyHit:
    amount: float
    start: int
    end: int
    raw: str


def find_money(text: str) -> list[MoneyHit]:
    """All ``$``-prefixed amounts in running text, in order, with character spans."""
    hits = []
    for m in _MONEY_FIND.finditer(text):
        if bool(m["open"]) != bool(m["close"]):
            continue
        amount = _to_amount(m["num"], m["mult"], negative=bool(m["open"]) or bool(m["neg"]))
        raw = m.group(0)
        start = m.start() + (len(raw) - len(raw.lstrip()))
        end = m.end() - (len(raw) - len(raw.rstrip()))
        hits.append(MoneyHit(amount, start, end, raw.strip()))
    return hits


_GST_EX = re.compile(r"\b(?:ex(?:cl(?:uding|\.|usive)?)?\.?\s*(?:of\s+)?gst|gst\s*(?:ex(?:clusive|cl\.?)?))", re.IGNORECASE)
_GST_INC = re.compile(r"\b(?:inc(?:l(?:uding|\.|usive)?)?\.?\s*(?:of\s+)?gst|gst\s*(?:inc(?:lusive|l\.?)?))", re.IGNORECASE)


def gst_basis(text: str | None) -> str:
    """'ex' | 'inc' | 'unknown' from wording near a value. Conflicting wording is 'unknown'."""
    if not text:
        return "unknown"
    ex, inc = bool(_GST_EX.search(text)), bool(_GST_INC.search(text))
    if ex and not inc:
        return "ex"
    if inc and not ex:
        return "inc"
    return "unknown"


# ---- financial years ---------------------------------------------------------------------------
_FY_STRICT = re.compile(r"(?<![\d/.,$])(?<!\d-)(?:(?P<fy>FY\s*)?(?P<a>20\d{2}|\d{2})\s*[-–—/]\s*(?P<b>20\d{2}|\d{2}))(?![\d/-])|"
                        r"(?<![\w/])FY\s*(?P<single>20\d{2}|\d{2})(?![\d/-])", re.IGNORECASE)


def _yr(s: str) -> int:
    return int(s) if len(s) == 4 else 2000 + int(s)


def parse_fy_all(text: str | None) -> list[str]:
    out = []
    for m in _FY_STRICT.finditer(text or ""):
        if m["single"]:
            a = _yr(m["single"])
            out.append(f"{a - 1}-{str(a)[2:]}")  # "FY26" = year ending June 2026 = 2025-26
            continue
        a, b = _yr(m["a"]), _yr(m["b"])
        if b == a + 1 and 2000 <= a <= 2099:
            out.append(f"{a}-{str(b)[2:]}")
    return out


def parse_fy(text: str | None) -> str | None:
    """First consecutive financial year in ``text`` as ``YYYY-YY`` ('2026-27', '26/27', 'FY2026-27')."""
    found = parse_fy_all(text)
    return found[0] if found else None


def strip_fy(text: str) -> str:
    """Remove FY suffixes such as ``26/27`` or ``2026-27`` (PLAN §5.2)."""
    def repl(m: re.Match) -> str:
        if m["single"]:
            return " "
        a, b = _yr(m["a"]), _yr(m["b"])
        return " " if b == a + 1 else m.group(0)
    return _FY_STRICT.sub(repl, text)


# ---- roads -------------------------------------------------------------------------------------
ROAD_ABBREVIATIONS = {
    "rd": "road", "st": "street", "dr": "drive", "ave": "avenue", "av": "avenue", "blvd": "boulevard",
    "pde": "parade", "cres": "crescent", "hwy": "highway", "pl": "place", "ct": "court", "tce": "terrace",
    "cl": "close", "ln": "lane", "pwy": "parkway", "esp": "esplanade", "cct": "circuit",
}
_ROAD_TYPES = ("road street drive avenue boulevard parade crescent highway place court terrace close lane way "
               "parkway esplanade circuit loop mews rise grove link walk bend crossing promenade").split()
_ROAD_TYPE_RE = "|".join(sorted(set(_ROAD_TYPES) | set(ROAD_ABBREVIATIONS), key=len, reverse=True))

DIRECTIONS = {
    "westbound": "westbound", "eastbound": "eastbound", "northbound": "northbound", "southbound": "southbound",
    "wb": "westbound", "eb": "eastbound", "nb": "northbound", "sb": "southbound",
}
_DIRECTION = re.compile(r"\(?\b(westbound|eastbound|northbound|southbound|wb|eb|nb|sb)\b\)?", re.IGNORECASE)


def extract_direction(text: str) -> tuple[str, str | None]:
    """Split a direction qualifier out of ``text``: ('Warwick Road (westbound)') -> ('Warwick Road', 'westbound')."""
    m = _DIRECTION.search(text)
    if not m:
        return text, None
    # Only treat 2-letter codes as directions when bracketed; "SB" alone is too ambiguous.
    word = m.group(1).lower()
    if len(word) == 2 and not m.group(0).startswith("("):
        return text, None
    cleaned = (text[: m.start()] + " " + text[m.end():]).strip()
    return re.sub(r"\s+", " ", cleaned), DIRECTIONS[word]


def normalise_road(text: str | None) -> str:
    """Lower-case, strip FY and direction, expand abbreviations (whole tokens), drop punctuation, collapse spaces."""
    if not text:
        return ""
    s, _ = extract_direction(strip_fy(text))
    s = s.replace("’", "'").replace("–", "-").replace("—", "-").replace("−", "-")
    s = re.sub(r"[^\w\s'-]", " ", s.lower())
    s = s.replace("'", "").replace("-", " ")
    tokens = s.split()
    out = []
    for i, tok in enumerate(tokens):
        if tok in ROAD_ABBREVIATIONS and not (tok == "st" and i == 0):  # leading "St" is usually Saint
            tok = ROAD_ABBREVIATIONS[tok]
        out.append(tok)
    return " ".join(out)


def normalise_road_with_direction(text: str | None) -> tuple[str, str | None]:
    if not text:
        return "", None
    return normalise_road(text), extract_direction(strip_fy(text))[1]


normalise_name = normalise_road  # same rules apply to project names used for fuzzy matching


# ---- segments and intersections ------------------------------------------------------------------
_ROAD_TOKEN = rf"[A-Z][\w'’.]*(?:[ \t]+[A-Z][\w'’.]*){{0,3}}?[ \t]+(?i:{_ROAD_TYPE_RE})\b\.?"
_SEG_DASH = re.compile(
    rf"(?P<road>{_ROAD_TOKEN})\s*(?:\((?P<dir>[^)]*)\))?\s*[–—-]\s*(?P<from>.+?)\s+(?i:to)\s+(?P<to>[^,;.\n]+?)\s*(?:[.;,)]|$)",
)
_SEG_BETWEEN = re.compile(
    rf"(?P<road>{_ROAD_TOKEN})\s*(?:\((?P<dir>[^)]*)\))?,?\s*(?i:between)\s+(?P<from>.+?)\s+(?i:and)\s+(?P<to>[^,;.\n]+?)\s*(?:[.;,)]|$)",
)


@dataclass(frozen=True)
class Segment:
    road: str
    from_road: str
    to_road: str
    direction: str | None
    raw: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.road, self.from_road, self.to_road)


def parse_segment(text: str | None) -> Segment | None:
    """Road segment: 'Elderberry Dr – South Lake to Berrigan Dr' or 'X Rd between A and B'. Values are normalised."""
    if not text:
        return None
    for pattern in (_SEG_DASH, _SEG_BETWEEN):
        m = pattern.search(text.strip())
        if m:
            road_raw = m["road"]
            d = extract_direction(f"({m['dir']})")[1] if m["dir"] else extract_direction(road_raw)[1]
            return Segment(
                road=normalise_road(road_raw),
                from_road=normalise_road(m["from"]),
                to_road=normalise_road(m["to"]),
                direction=d,
                raw=m.group(0).strip(),
            )
    return None


_INTERSECTION_PREFIX = re.compile(r"^\s*(?:the\s+)?(?:intersection|junction|roundabout)\s+(?:of|at)\s+", re.IGNORECASE)
_INTERSECTION_SUFFIX = re.compile(r"\s+(?:intersection|junction|roundabout|upgrade|black spot)\b.*$", re.IGNORECASE)
_SPLIT = re.compile(r"\s*(?:/|&|\band\b|\bx\b|\bwith\b)\s*", re.IGNORECASE)
_HAS_ROAD_TYPE = re.compile(rf"\b(?:{_ROAD_TYPE_RE})\b\.?$", re.IGNORECASE)


def parse_intersection(text: str | None) -> tuple[str, str] | None:
    """Order-independent pair of normalised road names, or None. 'Hepburn Ave / Marmion Ave roundabout'."""
    if not text:
        return None
    s = _INTERSECTION_PREFIX.sub("", text.strip())
    s = _INTERSECTION_SUFFIX.sub("", s)
    parts = [p for p in _SPLIT.split(s) if p.strip()]
    if len(parts) != 2:
        return None
    if not all(_HAS_ROAD_TYPE.search(p.strip()) for p in parts):
        return None
    a, b = (normalise_road(p) for p in parts)
    if not a or not b or a == b:
        return None
    return tuple(sorted((a, b)))  # type: ignore[return-value]


# ---- suburbs -----------------------------------------------------------------------------------
@lru_cache(maxsize=None)
def load_suburbs(council: str, directory: str | None = None) -> tuple[str, ...]:
    path = Path(directory) / f"{council}.txt" if directory else SUBURBS_DIR / f"{council}.txt"
    if not path.exists():
        return ()
    names = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]
    return tuple(dict.fromkeys(names))


def all_suburbs() -> dict[str, tuple[str, ...]]:
    return {p.stem: load_suburbs(p.stem) for p in sorted(SUBURBS_DIR.glob("*.txt"))}


def lookup_suburb(text: str | None, council: str | None = None) -> str | None:
    """Longest whole-word suburb name found in ``text`` (case-insensitive), from one council's list or all."""
    if not text:
        return None
    names = load_suburbs(council) if council else tuple(n for v in all_suburbs().values() for n in v)
    best = None
    for name in names:
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.IGNORECASE) and (best is None or len(name) > len(best)):
            best = name
    return best
