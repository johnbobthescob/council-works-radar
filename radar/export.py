"""UI export (PLAN.md §7.2): ``out/ui/data.js`` (``window.RADAR_DATA = {...};``) plus the static UI files.

``data.js`` carries facts, <= 300-character quotes and source links only. A ``.js`` data file (not ``fetch``)
means ``index.html`` also works when opened straight from ``file://``.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from radar import changes, db
from radar.digest import BLACKSPOT_NOTE, FOOTER_TEXT, evidence_mention, project_funding, NONCOMMERCIAL_NOTE, load_data, primary_trade, short_quote, source_href
from radar.models import ChangeEvent, Run
from radar.settings import OUT_DIR

UI_SOURCE_DIR = Path(__file__).parent / "ui"
UI_FILES = ("index.html", "app.js", "style.css")
DATA_PREFIX = "window.RADAR_DATA = "
MAX_BYTES = 5 * 1024 * 1024


def build_data(conn: sqlite3.Connection, *, now: datetime | None = None, blackspot_enabled: bool = False) -> dict:
    data = load_data(conn)
    events: dict[str, list[ChangeEvent]] = {}
    for r in conn.execute("SELECT * FROM change_event ORDER BY created_at, event_id"):
        events.setdefault(r["project_id"], []).append(db._from_row(ChangeEvent, r))
    runs = {r.run_id: r for r in (db._from_row(Run, row) for row in conn.execute("SELECT * FROM run"))}
    latest_run = max(runs.values(), key=lambda r: r.started_at, default=None)

    projects = []
    for p in sorted(data.projects.values(), key=lambda p: (p.council_id, p.canonical_name or "", p.project_id)):
        sources = []
        rule_codes: set[str] = set()
        for mid in sorted(data.by_project.get(p.project_id, [])):
            m = data.mentions.get(mid)
            doc = data.docs.get(m.doc_id) if m else None
            if not m or not doc:
                continue
            rule_codes |= {lab["code"] for lab in m.trade_labels or [] if lab.get("method") != "rule_implied"}
            sources.append({
                "mention_id": m.mention_id, "title": doc.title or doc.doc_type, "doc_type": doc.doc_type,
                "url": source_href(doc, m.page), "page": m.page,
                "retrieved_at": doc.retrieved_at or doc.last_checked_at or "", "meeting_date": doc.meeting_date,
                "quote": short_quote(m.quote), "extractor": m.extractor, "extractor_version": m.extractor_version,
                "flags": m.uncertainty_flags or [], "value": m.value_amount, "value_basis": m.value_basis,
                "stage": m.stage, "name": m.project_name_raw, "code": m.council_project_code, "confidence": m.confidence,
            })
        evs = events.get(p.project_id, [])
        history = [{"type": e.event_type, "at": e.created_at, "run_id": e.run_id, "old": e.old, "new": e.new} for e in evs]
        last = evs[-1] if evs else None
        projects.append({
            "id": p.project_id, "council": p.council_id, "name": p.canonical_name, "code": next(
                (s["code"] for s in sources if s["code"]), None),
            "road": p.road_name, "from": p.from_road, "to": p.to_road, "suburb": p.suburb,
            "trade": primary_trade(data, p), "trades": [c for c in p.trade_codes if c in rule_codes or c == "UNKNOWN"],
            "implied": [c for c in p.trade_codes if c not in rule_codes and c != "UNKNOWN"],
            "value": p.latest_value, "value_basis": p.latest_value_basis, "stage": p.latest_stage, "fy": p.financial_year,
            "first_seen": p.first_seen, "last_seen": p.last_seen, "confidence": p.confidence, "flags": p.flags,
            "review_needed": p.review_needed, "last_change": {"type": last.event_type, "at": last.created_at} if last else None,
            "funding": project_funding(data, p, evidence_mention(data, p)),
            "history": history, "sources": sources,
        })
    return {
        "generated_at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "iso_week": latest_run.iso_week if latest_run else changes.iso_week(),
        "baseline": bool(latest_run and latest_run.counts.get("baseline")),
        "councils": [{"id": k, "name": v} for k, v in data.council_names.items()
                     if any(p["council"] == k for p in projects)],
        "trades": [{"code": k, "label": data.trade_labels[k]} for k in data.trade_order],
        "footer": FOOTER_TEXT, "noncommercial": NONCOMMERCIAL_NOTE,
        "blackspot_note": BLACKSPOT_NOTE if blackspot_enabled else None, "projects": projects,
    }


def render_data_js(payload: dict) -> str:
    # "</" is escaped so the file can never close a surrounding <script> element
    return DATA_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/") + ";\n"


def parse_data_js(text: str) -> dict:
    """Inverse of ``render_data_js`` (used by tests and handy for debugging)."""
    body = text.strip()
    assert body.startswith(DATA_PREFIX) and body.endswith(";"), "unexpected data.js framing"
    return json.loads(body[len(DATA_PREFIX):-1])


def export_ui(conn: sqlite3.Connection, out_dir: Path | str | None = None, *, now: datetime | None = None,
              blackspot_enabled: bool = False) -> Path:
    """Write ``<out>/ui/{index.html,app.js,style.css,data.js}``; returns the ui directory."""
    ui = Path(out_dir or OUT_DIR) / "ui"
    ui.mkdir(parents=True, exist_ok=True)
    for name in UI_FILES:
        shutil.copyfile(UI_SOURCE_DIR / name, ui / name)
    raw = render_data_js(build_data(conn, now=now, blackspot_enabled=blackspot_enabled)).encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise ValueError(f"data.js is {len(raw):,} bytes, over the {MAX_BYTES:,}-byte budget")
    (ui / "data.js").write_bytes(raw)
    return ui
