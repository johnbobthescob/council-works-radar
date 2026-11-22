"""Weekly digest (PLAN.md §7.1): one self-contained HTML file (inline CSS, no JS) plus a PDF of the same page.

The digest holds facts, short quotes (<= 300 chars) and links to the official source only; council documents
are never reproduced. PDF: WeasyPrint if importable and working, else ``pymupdf.Story``; if both fail the HTML
is still written and the failure is logged and returned.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import jinja2

from radar import changes, db
from radar.models import ChangeEvent, Project, ProjectMention, Run, SourceDocument
from radar.settings import OUT_DIR, MAX_QUOTE_CHARS, load_councils, load_trades

log = logging.getLogger(__name__)
TEMPLATE_DIR = Path(__file__).parent / "templates"

BASELINE_PER_TRADE = 15
PIPELINE_PER_TRADE = 25
LOW_CONFIDENCE = 0.5
HIGH_CONFIDENCE = 0.75
REVIEW_LIST_LIMIT = 25
NOT_LISTED_TRADES = ("NON_CIVIL", "UNKNOWN")      # counted, but not listed as cards (the UI has them all)
PRE_TENDER_STAGES = ("proposed_budget", "adopted_budget", "budget_review", "tender_advertised", None)

FOOTER_TEXT = (
    "Council material © the respective City. Facts are extracted from public documents and link to the official "
    "source; documents are not reproduced. Verify against the source before relying on any figure. Values are as "
    "stated in source documents; GST basis may be unknown."
)
NONCOMMERCIAL_NOTE = (
    "Some councils (Cockburn, Melville, Perth) limit reuse of their website material to personal or non-commercial "
    "use, and Rockingham requires written permission to reproduce material. This digest is a private research pilot "
    "that republishes facts and links only."
)
BLACKSPOT_NOTE = (
    "Black Spot funding references come from Main Roads WA material whose reuse licence has not been verified; "
    "they are used only to tag council projects."
)
FLAG_LABELS = {
    "value_ambiguous_column": "value column ambiguous", "wrapped_text_join": "wrapped text", "name_only_no_location":
    "name only, no location", "multi_year": "multi-year", "carry_forward": "carry forward",
    "possible_duplicate": "possible duplicate", "ocr_used": "OCR used", "ocr_needed": "OCR needed",
    "value_missing": "value missing", "gst_unknown": "GST unknown", "agenda_not_resolution": "agenda, not yet resolved",
}
BASIS_LABELS = {"budget_total": "budget total", "current_year": "current year", "municipal": "municipal share",
                "grant": "grant", "contract_award": "contract award", "estimate": "estimate"}
GST_LABELS = {"ex": "ex GST", "inc": "inc GST", "unknown": "GST unknown"}
DOC_TYPE_LABELS = {"budget": "Budget", "capital_program": "Capital works program", "project_plans": "Project plans",
                   "agenda": "Agenda", "minutes": "Minutes", "attachments": "Attachments",
                   "tender_register": "Tender register", "blackspot_list": "Black Spot list", "other": "Document"}


# ---- formatting helpers ------------------------------------------------------------------------
def money(v: float | None) -> str:
    return "—" if v is None else f"${v:,.0f}"


def short_quote(q: str | None, limit: int = MAX_QUOTE_CHARS) -> str:
    q = " ".join((q or "").split())
    return q if len(q) <= limit else q[: limit - 1].rstrip() + "…"


def confidence_label(score: float) -> str:
    return "High" if score >= HIGH_CONFIDENCE else "Medium" if score >= LOW_CONFIDENCE else "Low"


def week_bounds(week: str) -> tuple[date, date]:
    year, wk = week.split("-W")
    return date.fromisocalendar(int(year), int(wk), 1), date.fromisocalendar(int(year), int(wk), 7)


def week_title(week: str) -> str:
    start, end = week_bounds(week)
    span = (f"{start.day}–{end.day} {end:%b}" if start.month == end.month else f"{start.day} {start:%b} – {end.day} {end:%b}")
    return f"Week {int(week.split('-W')[1])}, {week.split('-W')[0]} ({span})"


def source_href(doc: SourceDocument, page: int | None) -> str:
    is_pdf = "pdf" in (doc.content_type or "").lower() or ".pdf" in doc.source_url.lower()
    return f"{doc.source_url}#page={page}" if is_pdf and page else doc.source_url


# ---- data loading ------------------------------------------------------------------------------
@dataclass
class Data:
    """Everything the digest needs from the database, loaded once."""
    projects: dict[str, Project]
    mentions: dict[str, ProjectMention]
    docs: dict[str, SourceDocument]
    by_project: dict[str, list[str]]
    council_names: dict[str, str]
    trade_labels: dict[str, str]
    trade_order: list[str]


def load_data(conn: sqlite3.Connection) -> Data:
    councils, trades = load_councils(), load_trades()
    by_project: dict[str, list[str]] = {}
    for link in db.all_links(conn):
        by_project.setdefault(link.project_id, []).append(link.mention_id)
    return Data(
        projects={p.project_id: p for p in db.all_projects(conn)},
        mentions={m.mention_id: m for m in db.all_mentions(conn)},
        docs={d.doc_id: d for d in db.all_source_documents(conn)},
        by_project=by_project,
        council_names={k: c.name for k, c in councils.councils.items()},
        trade_labels={k: t.label for k, t in trades.trades.items()},
        trade_order=list(trades.trades),
    )


def primary_trade(data: Data, p: Project) -> str:
    """First rule-matched trade in taxonomy order; implied-only and unlabelled projects fall back."""
    rule, implied = set(), set()
    for mid in data.by_project.get(p.project_id, []):
        for lab in data.mentions[mid].trade_labels or []:
            (implied if lab.get("method") == "rule_implied" else rule).add(lab["code"])
    for pool in (rule - {"UNKNOWN"}, implied, set(p.trade_codes) - {"UNKNOWN"}):
        for code in data.trade_order:
            if code in pool:
                return code
    return "UNKNOWN"


def trade_chips(data: Data, p: Project) -> list[dict]:
    rule = {lab["code"] for mid in data.by_project.get(p.project_id, []) for lab in data.mentions[mid].trade_labels or []
            if lab.get("method") != "rule_implied"}
    codes = [c for c in data.trade_order if c in set(p.trade_codes)]
    return [{"code": c, "label": c.replace("_", " ").title(), "implied": c not in rule} for c in codes]


def evidence_mention(data: Data, p: Project, preferred: str | None = None) -> ProjectMention | None:
    ms = [data.mentions[i] for i in data.by_project.get(p.project_id, []) if i in data.mentions]
    if not ms:
        return None
    if preferred and preferred in data.mentions:
        return data.mentions[preferred]
    valued = [m for m in ms if m.value_amount == p.latest_value and p.latest_value is not None] or ms
    return max(valued, key=lambda m: (m.confidence, m.mention_id))


def change_lines(events: list[ChangeEvent]) -> list[dict]:
    out = []
    for e in events:
        old, new = e.old or {}, e.new or {}
        if e.event_type == "new":
            text = "NEW"
        elif e.event_type == "value_changed":
            delta = new.get("delta") or 0
            pct = new.get("pct")
            arrow = "▲" if delta >= 0 else "▼"
            text = f"VALUE {arrow} {money(abs(delta))}" + (f" ({pct:+.0%})" if pct is not None else "") + \
                   f" — {money(old.get('value'))} → {money(new.get('value'))}"
        elif e.event_type == "stage_advanced":
            text = f"STAGE {old.get('stage') or 'none'} → {new.get('stage')}"
        elif e.event_type in ("deferred", "deleted"):
            text = e.event_type.upper() + (" (carry forward)" if new.get("carry_forward") else "")
        elif e.event_type == "location_refined":
            text = "LOCATION " + ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in new.items())
        elif e.event_type == "source_added":
            text = f"NEW SOURCE ({new.get('source_count')} documents now mention this project)"
        else:
            text = e.event_type.upper()
        out.append({"type": e.event_type, "text": text})
    return out


def build_item(data: Data, p: Project, events: list[ChangeEvent] | None = None) -> dict:
    preferred = next((e.mention_id for e in events or [] if e.mention_id), None)
    m = evidence_mention(data, p, preferred)
    doc = data.docs.get(m.doc_id) if m else None
    council = data.council_names.get(p.council_id, p.council_id)
    fy = p.financial_year or (m.financial_year if m else None)
    value_text = None
    if p.latest_value is not None:
        bits = [BASIS_LABELS.get(p.latest_value_basis or "", p.latest_value_basis or "basis not stated")]
        if fy:
            bits.append(f"FY{fy}")
        bits.append(GST_LABELS.get(m.value_gst if m else "unknown", "GST unknown"))
        value_text = f"{money(p.latest_value)} ({', '.join(bits)})"
    source = None
    if m and doc:
        title = doc.title or " ".join(x for x in (DOC_TYPE_LABELS.get(doc.doc_type, "Document"), doc.financial_year) if x)
        retrieved = (doc.retrieved_at or "")[:10] or "date unknown"
        source = {"label": f"{council}, {title}, p. {m.page}, retrieved {retrieved}", "url": source_href(doc, m.page),
                  "host": urlsplit(doc.source_url).netloc}
    dates = " – ".join(x for x in ((m.date_start or m.date_stated) if m else None, m.date_end if m else None) if x) or None
    flags = [FLAG_LABELS.get(f, f.replace("_", " ")) for f in p.flags]
    return {
        "project_id": p.project_id, "name": p.canonical_name or (m.project_name_raw if m else "(unnamed)"),
        "council_id": p.council_id, "council": council, "suburb": p.suburb or (m.suburb if m else None),
        "ward": m.ward if m else None, "road": p.road_name, "trade": primary_trade(data, p),
        "chips": trade_chips(data, p), "value": p.latest_value, "value_text": value_text,
        "funding": " / ".join(m.funding_refs) if m and m.funding_refs else None,
        "stage": p.latest_stage, "changes": change_lines(events or []), "dates": dates,
        "quote": short_quote(m.quote) if m else "", "source": source, "confidence": p.confidence,
        "confidence_label": confidence_label(p.confidence), "flags": flags, "review_needed": p.review_needed,
        "code": m.council_project_code if m else None,
    }


# ---- sections ----------------------------------------------------------------------------------
def _by_value(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda i: (-(i["value"] or 0), i["name"], i["project_id"]))


def group_by_trade_council(items: list[dict], data: Data) -> list[dict]:
    groups = []
    for code in data.trade_order:
        in_trade = [i for i in items if i["trade"] == code]
        if not in_trade:
            continue
        councils = []
        for cid in sorted({i["council_id"] for i in in_trade}, key=lambda c: data.council_names.get(c, c)):
            councils.append({"name": data.council_names.get(cid, cid), "entries": _by_value([i for i in in_trade if i["council_id"] == cid])})
        groups.append({"code": code, "label": data.trade_labels.get(code, code), "count": len(in_trade), "councils": councils})
    return groups


def baseline_section(data: Data) -> dict:
    items = [build_item(data, p) for p in data.projects.values()]
    matrix: dict[str, dict[str, int]] = {}
    for i in items:
        matrix.setdefault(i["council_id"], {}).setdefault(i["trade"], 0)
        matrix[i["council_id"]][i["trade"]] += 1
    trades_present = [t for t in data.trade_order if any(t in row for row in matrix.values())]
    rows = [{"council": data.council_names.get(c, c), "counts": [matrix[c].get(t, 0) for t in trades_present],
             "total": sum(matrix[c].values())} for c in sorted(matrix, key=lambda c: data.council_names.get(c, c))]
    top = []
    for code in data.trade_order:
        if code in NOT_LISTED_TRADES:
            continue
        in_trade = _by_value([i for i in items if i["trade"] == code])
        if in_trade:
            top.append({"code": code, "label": data.trade_labels.get(code, code), "total": len(in_trade),
                        "entries": in_trade[:BASELINE_PER_TRADE]})
    return {"matrix_headers": [data.trade_labels.get(t, t) for t in trades_present], "matrix_rows": rows,
            "top": top, "project_count": len(items), "per_trade": BASELINE_PER_TRADE}


def pipeline_section(data: Data) -> list[dict]:
    out = []
    for code in data.trade_order:
        if code in NOT_LISTED_TRADES:
            continue
        items = _by_value([build_item(data, p) for p in data.projects.values()
                           if p.latest_stage in PRE_TENDER_STAGES and primary_trade(data, p) == code])
        if items:
            out.append({"code": code, "label": data.trade_labels.get(code, code), "entries": items[:PIPELINE_PER_TRADE]})
    return out


def review_section(conn: sqlite3.Connection, data: Data) -> dict:
    dups = []
    for r in db.possible_duplicates(conn):
        a, b = data.projects.get(r["project_a"]), data.projects.get(r["project_b"])
        if a and b:
            dups.append({"a": build_item(data, a), "b": build_item(data, b), "method": r["method"], "score": r["score"]})
    low = _by_value([build_item(data, p) for p in data.projects.values() if p.confidence < LOW_CONFIDENCE])
    bad_docs = [{"title": d.title or d.source_url, "url": d.source_url, "status": d.extract_status,
                 "council": data.council_names.get(d.council_id, d.council_id)}
                for d in data.docs.values() if d.extract_status in ("failed", "ocr_needed", "partial", "skipped_size")]
    return {"duplicates": dups[:REVIEW_LIST_LIMIT], "duplicate_count": len(dups), "low": low[:REVIEW_LIST_LIMIT],
            "low_count": len(low), "documents": sorted(bad_docs, key=lambda d: (d["council"], d["title"]))}


def health_section(conn: sqlite3.Connection, data: Data, runs: list, since: str | None) -> dict:
    """Per-council document and fetch statistics from the DB (fetch_log rows since the week's first run)."""
    councils = load_councils()
    host_council = {h: k for k, c in councils.councils.items() for h in c.hosts}
    rows: dict[str, dict] = {}

    def row(cid: str) -> dict:
        return rows.setdefault(cid, {"council": data.council_names.get(cid, cid), "docs": 0, "last": "", "status": {},
                                     "requests": 0, "fetched": 0, "unchanged": 0, "blocked": 0, "failed": 0,
                                     "robots": "not checked", "paused": False})

    for cid in {c for r in runs for c in r.councils} | {d.council_id for d in data.docs.values()}:
        row(cid)
    for d in data.docs.values():
        r = row(d.council_id)
        r["docs"] += 1
        r["last"] = max(r["last"], (d.retrieved_at or "")[:16].replace("T", " "))
        r["status"][d.extract_status or "unknown"] = r["status"].get(d.extract_status or "unknown", 0) + 1
    sql, args = "SELECT * FROM fetch_log", ()
    if since:
        sql, args = sql + " WHERE ts >= ?", (since,)
    for f in conn.execute(sql, args):
        cid = host_council.get(f["host"])
        if cid is None:
            continue
        r = row(cid)
        if f["url"].endswith("/robots.txt"):
            r["robots"] = "allowed" if f["status"] in (200, 404) else f"status {f['status']}"
            continue
        r["requests"] += 1
        note = (f["note"] or "")
        if f["status"] in (403, 429) or "paused" in note or "challenge" in note:
            r["paused"] = True
        if f["status"] == 304 or f["cache_hit"]:
            r["unchanged"] += 1
        elif f["status"] == 200 and f["method"] == "GET":
            r["fetched"] += 1
        elif any(w in note for w in ("blocklist", "robots", "paused", "size")):
            r["blocked"] += 1
        elif f["status"] is None or f["status"] >= 400:
            r["failed"] += 1
    for r in rows.values():
        r["status_text"] = ", ".join(f"{k} {v}" for k, v in sorted(r["status"].items())) or "—"
    errors = [e for run in runs for e in run.errors]
    return {"councils": sorted(rows.values(), key=lambda r: r["council"]), "errors": errors}


# ---- build + render ----------------------------------------------------------------------------
def pick_week(conn: sqlite3.Connection, week: str | None) -> str:
    if week:
        return week
    row = conn.execute("SELECT iso_week FROM run WHERE iso_week IS NOT NULL ORDER BY started_at DESC LIMIT 1").fetchone()
    return row[0] if row else changes.iso_week()


def build_context(conn: sqlite3.Connection, week: str | None = None, *, now: datetime | None = None,
                  blackspot_enabled: bool = False) -> dict:
    week = pick_week(conn, week)
    data = load_data(conn)
    run_rows = conn.execute("SELECT * FROM run WHERE iso_week = ? ORDER BY started_at", (week,)).fetchall()
    runs = [db._from_row(Run, r) for r in run_rows]
    events = changes.events_for_runs(conn, [r.run_id for r in runs])
    baseline = any(r.counts.get("baseline") for r in runs)
    ctx: dict = {
        "week": week, "title": week_title(week), "generated": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC"),
        "baseline": baseline, "runs": runs, "run_count": len(runs), "project_count": len(data.projects),
        "footer_text": FOOTER_TEXT, "noncommercial_note": NONCOMMERCIAL_NOTE,
        "blackspot_note": BLACKSPOT_NOTE if blackspot_enabled else None,
        "flag_labels": FLAG_LABELS, "event_count": len(events),
    }
    ctx["health"] = health_section(conn, data, runs, runs[0].started_at if runs else None)
    ctx["review"] = review_section(conn, data)
    if baseline:
        ctx["baseline_section"] = baseline_section(data)
        ctx["signals"], ctx["pipeline"] = [], []
        # incremental events can sit in the same week as the baseline run; they are shown too
        non_new = [e for e in events if e.event_type != "new" and not any(
            r.run_id == e.run_id and r.counts.get("baseline") for r in runs)]
    else:
        non_new = events
    per_project: dict[str, list[ChangeEvent]] = {}
    for e in non_new:
        if e.project_id in data.projects:
            per_project.setdefault(e.project_id, []).append(e)
    items = [build_item(data, data.projects[pid], evs) for pid, evs in per_project.items()]
    ctx["signals"] = group_by_trade_council(items, data)
    ctx["signal_count"] = len(items)
    if not baseline:
        ctx["pipeline"] = pipeline_section(data)
    return ctx


def render_html(ctx: dict) -> str:
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATE_DIR), autoescape=True, trim_blocks=True, lstrip_blocks=True)
    env.filters["money"] = money
    css = (TEMPLATE_DIR / "digest.css").read_text(encoding="utf-8")
    return env.get_template("digest.html.j2").render(css=css, **ctx)


# ---- PDF ---------------------------------------------------------------------------------------
def html_to_pdf(html: str, css: str, target: Path, engine: str = "auto") -> str:
    """Write ``target``; return the engine used ('weasyprint' | 'story'). Raises RuntimeError if none worked."""
    errors = []
    if engine in ("auto", "weasyprint"):
        try:
            import weasyprint

            weasyprint.HTML(string=html).write_pdf(str(target))
            return "weasyprint"
        except Exception as exc:   # ImportError, missing pango, render error
            errors.append(f"weasyprint: {type(exc).__name__}: {exc}")
            log.warning("WeasyPrint failed (%s); trying pymupdf Story", exc)
    if engine in ("auto", "story"):
        try:
            _story_pdf(html, css, target)
            return "story"
        except Exception as exc:
            errors.append(f"story: {type(exc).__name__}: {exc}")
    raise RuntimeError("; ".join(errors))


def _story_pdf(html: str, css: str, target: Path) -> None:
    """PyMuPDF Story fallback (no system libraries). ``@page`` rules are print-only and unsupported here."""
    import re

    import pymupdf

    css = re.sub(r"@page\s*\{(?:[^{}]|\{[^{}]*\})*\}", "", css)
    html = re.sub(r"<style>.*?</style>", "", html, flags=re.S)
    page = pymupdf.paper_rect("a4")
    body = page + (36, 36, -36, -40)
    story = pymupdf.Story(html=html, user_css=css)
    pdf = story.write_with_links(lambda _num, _filled: (page, body, None))     # layout + link annotations
    try:
        pdf.save(str(target))
    finally:
        pdf.close()


@dataclass
class DigestResult:
    week: str
    html_path: Path
    pdf_path: Path | None = None
    pdf_engine: str | None = None
    pdf_error: str | None = None
    context: dict = field(default_factory=dict, repr=False)


def write_digest(conn: sqlite3.Connection, out_dir: Path | str | None = None, week: str | None = None, *,
                 pdf: bool = True, engine: str = "auto", now: datetime | None = None,
                 blackspot_enabled: bool = False) -> DigestResult:
    out = Path(out_dir) if out_dir else OUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    ctx = build_context(conn, week, now=now, blackspot_enabled=blackspot_enabled)
    html = render_html(ctx)
    html_path = out / f"digest-{ctx['week']}.html"
    html_path.write_text(html, encoding="utf-8")
    result = DigestResult(ctx["week"], html_path, context=ctx)
    if pdf:
        pdf_path = out / f"digest-{ctx['week']}.pdf"
        try:
            result.pdf_engine = html_to_pdf(html, (TEMPLATE_DIR / "digest.css").read_text(encoding="utf-8"), pdf_path, engine)
            result.pdf_path = pdf_path
        except RuntimeError as exc:
            result.pdf_error = str(exc)
            log.error("PDF not written (HTML is): %s", exc)
    return result
