"""`run` orchestration (PLAN.md §2.2, §3.3, §12.2): list -> select -> fetch -> extract -> link -> changes -> digest -> UI.

Failures are isolated per adapter and per document: an error is recorded in the run (and the run summary JSON) and
the run carries on with the other documents and councils. ``offline=True`` makes no network requests at all; it
re-extracts the documents already stored in the DB from their cached raw bytes in ``.cache/raw``.
"""

from __future__ import annotations

import importlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from radar import blackspot, changes, db, dedupe, digest, export
from radar.adapters import ADAPTER_MODULES
from radar.adapters.base import DocRef
from radar.extract import IMPLEMENTED
from radar.ingest import IngestResult, extract_document, fetch_and_extract
from radar.settings import DATA_DIR, OUT_DIR, TIMEZONE, Council, CouncilsConfig, load_councils

log = logging.getLogger(__name__)

ANNUAL_TYPES = ("budget", "capital_program", "project_plans", "blackspot_list")
MEETING_TYPES = ("minutes", "agenda")
FAILURE_PREFIXES = ("http_error", "challenge", "error", "paused")
OPTIONAL_EXTRACTORS = {"blackspot"}            # disabled unless named in ``enable`` (PLAN M17)


# ---- selection policy (PLAN §3.3) -------------------------------------------------------------
def _meeting_rank(ref: DocRef) -> tuple:
    """Sort key, best first: Ordinary Council, then minutes before agendas, then newest."""
    mt = f"{ref.meeting_type or ''} {ref.title or ''}".lower()
    ordinary = "ordinary" in mt or "ocm" in mt or mt.strip().startswith("oc ")
    return (not ordinary, ref.doc_type != "minutes", _neg_date(ref.meeting_date))


def _neg_date(iso: str | None) -> int:
    try:
        return -date.fromisoformat((iso or "")[:10]).toordinal()
    except ValueError:
        return 0


def select_docs(refs: list[DocRef], *, today: date, window_days: int, max_meeting_docs: int,
                enable: set[str] = frozenset()) -> tuple[list[DocRef], list[tuple[DocRef, str]]]:
    """Apply the document policy. Returns (selected, [(ref, reason_skipped)])."""
    selected: list[DocRef] = []
    skipped: list[tuple[DocRef, str]] = []
    seen: set[str] = set()
    meetings: list[DocRef] = []
    for ref in refs:
        if ref.doc_key in seen:
            continue
        seen.add(ref.doc_key)
        if not ref.extractor or ref.extractor not in IMPLEMENTED:
            skipped.append((ref, f"no implemented extractor ({ref.extractor or 'none'})"))
        elif ref.extractor in OPTIONAL_EXTRACTORS and ref.extractor not in enable:
            skipped.append((ref, f"{ref.extractor} disabled (use --enable {ref.extractor})"))
        elif ref.doc_type in MEETING_TYPES or ref.extractor == "agenda_items":
            meetings.append(ref)
        else:
            selected.append(ref)                 # annual docs: always (conditional GET keeps re-checks cheap)

    cutoff = today - timedelta(days=window_days)
    in_window: list[DocRef] = []
    for ref in meetings:
        try:
            when = date.fromisoformat((ref.meeting_date or "")[:10])
        except ValueError:
            skipped.append((ref, "meeting date unknown"))
            continue
        if when < cutoff or when > today + timedelta(days=14):
            skipped.append((ref, f"meeting {when} outside the {window_days}-day window"))
        elif ref.doc_type not in MEETING_TYPES:
            skipped.append((ref, f"doc_type {ref.doc_type} is not an agenda or minutes"))
        else:
            in_window.append(ref)
    # Prefer minutes over the agenda of the same meeting (minutes confirm decisions).
    have_minutes = {(r.meeting_date, (r.meeting_type or "").lower()) for r in in_window if r.doc_type == "minutes"}
    ranked = []
    for ref in sorted(in_window, key=_meeting_rank):
        if ref.doc_type == "agenda" and (ref.meeting_date, (ref.meeting_type or "").lower()) in have_minutes:
            skipped.append((ref, "minutes of the same meeting preferred"))
        else:
            ranked.append(ref)
    selected += ranked[:max_meeting_docs]
    skipped += [(r, f"over the {max_meeting_docs} meeting docs/run cap") for r in ranked[max_meeting_docs:]]
    return selected, skipped


# ---- per-council stages -----------------------------------------------------------------------
@dataclass
class CouncilResult:
    council: str
    listed: int = 0
    selected: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)
    mentions: int = 0
    docs: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def add(self, res: IngestResult) -> None:
        self.outcomes[res.outcome] = self.outcomes.get(res.outcome, 0) + 1
        self.mentions += res.mentions
        self.docs.append({"url": res.ref.url, "doc_type": res.ref.doc_type, "title": res.ref.title,
                          "meeting_date": res.ref.meeting_date, "extractor": res.ref.extractor,
                          "outcome": res.outcome, "mentions": res.mentions, "detail": res.detail})
        # hard failures go in the errors list; policy skips (robots, blocklist, size) stay in the documents list
        if res.outcome == "error" or (res.outcome == "skipped" and (res.detail or "").startswith(FAILURE_PREFIXES)):
            self.errors.append(f"{res.ref.url}: {res.detail}")


def list_refs(council: Council, fetcher, result: CouncilResult) -> list[DocRef]:
    refs: list[DocRef] = []
    for adapter in council.adapters:
        try:
            module = importlib.import_module(ADAPTER_MODULES[adapter.type])
            got = module.list_docs(council, adapter.raw, fetcher)
        except Exception as exc:              # one adapter failing must not stop the council or the run
            log.warning("%s: adapter %s failed: %s", council.key, adapter.type, exc)
            result.errors.append(f"adapter {adapter.type}: {type(exc).__name__}: {exc}")
            continue
        for ref in got:
            ref.extractor = ref.extractor or adapter.raw.get("extractor")
            ref.adapter = ref.adapter or adapter.type
            refs.append(ref)
    return refs


def save_listing(council_id: str, refs: list[DocRef], today: date, data_dir: Path | None) -> None:
    if data_dir is None:
        return
    folder = Path(data_dir) / "listings" / council_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{today.isoformat()}.json").write_text(
        json.dumps([r.to_dict() for r in refs], indent=1, ensure_ascii=False), encoding="utf-8")


def _extractor_for(conn: sqlite3.Connection, doc, config: CouncilsConfig) -> str | None:
    row = conn.execute("SELECT extractor FROM project_mention WHERE doc_id = ? AND extractor IS NOT NULL LIMIT 1",
                       (doc.doc_id,)).fetchone()
    if row:
        return row[0]
    council = config.councils.get(doc.council_id)
    for adapter in council.adapters if council else []:
        for d in adapter.raw.get("docs", []) or []:
            if d.get("url") == doc.source_url:
                return d.get("extractor")
        if doc.doc_type == "project_plans" and adapter.raw.get("discover_extractor"):
            return adapter.raw["discover_extractor"]
    return "agenda_items" if doc.doc_type in MEETING_TYPES else None


def replay_council(conn: sqlite3.Connection, council: Council, fetcher, config: CouncilsConfig,
                   result: CouncilResult) -> None:
    """Offline: re-extract this council's current documents from ``.cache/raw`` (no network)."""
    docs = [d for d in db.all_source_documents(conn) if d.council_id == council.key and not d.superseded_by]
    result.listed = result.selected = len(docs)
    for doc in docs:
        ref = DocRef(council_id=doc.council_id, url=doc.source_url, doc_type=doc.doc_type, title=doc.title,
                     meeting_date=doc.meeting_date, financial_year=doc.financial_year, listing_url=doc.listing_url,
                     extractor=_extractor_for(conn, doc, config))
        path = fetcher.cached_raw(doc.doc_id, doc.source_url)
        if not ref.extractor or ref.extractor not in IMPLEMENTED:
            result.add(IngestResult(ref, "unsupported", detail="no extractor known for this document"))
        elif path is None:
            result.add(IngestResult(ref, "skipped", detail="offline: raw bytes not in .cache/raw"))
        else:
            try:
                d, n = extract_document(conn, path, council_id=doc.council_id, url=doc.source_url, extractor=ref.extractor,
                                        doc_type=doc.doc_type, content_type=doc.content_type,
                                        financial_year=doc.financial_year, title=doc.title,
                                        meeting_date=doc.meeting_date, listing_url=doc.listing_url,
                                        cache_dir=fetcher.cache_dir)
                result.add(IngestResult(ref, "extracted", n, detail=f"{d.extract_status} (replayed from cache)"))
            except Exception as exc:
                result.add(IngestResult(ref, "error", detail=f"{type(exc).__name__}: {exc}"))


def run_council(conn: sqlite3.Connection, council: Council, fetcher, config: CouncilsConfig, *, today: date,
                allow_large: bool, enable: set[str], max_meeting_docs: int | None, data_dir: Path | None) -> CouncilResult:
    result = CouncilResult(council.key)
    refs = list_refs(council, fetcher, result)
    result.listed = len(refs)
    save_listing(council.key, refs, today, data_dir)
    d = config.defaults
    selected, skipped = select_docs(refs, today=today, window_days=d.meeting_window_days,
                                    max_meeting_docs=d.max_meeting_docs_per_run if max_meeting_docs is None else max_meeting_docs,
                                    enable=enable)
    result.selected = len(selected)
    result.skipped = [{"url": r.url, "doc_type": r.doc_type, "meeting_date": r.meeting_date, "reason": why}
                      for r, why in skipped]
    for ref in selected:
        try:
            res = fetch_and_extract(fetcher, conn, ref, allow_large=allow_large)
        except Exception as exc:              # extractor/text-layer crash on one doc: record, continue
            log.exception("%s: %s failed", council.key, ref.url)
            res = IngestResult(ref, "error", detail=f"{type(exc).__name__}: {exc}")
        result.add(res)
    return result


def cross_check_docs(config: CouncilsConfig, name: str) -> list[DocRef]:
    """Document refs of an optional cross-check source (``cross_checks.<name>`` in councils.yaml)."""
    cfg = config.cross_checks.get(name) or {}
    return [DocRef(council_id=dedupe.CROSS_CHECK_COUNCIL, url=d["url"], doc_type=d.get("doc_type", "other"),
                   title=d.get("title"), financial_year=d.get("financial_year"), fmt=d.get("format"),
                   extractor=d.get("extractor", name), adapter="cross_check") for d in cfg.get("docs") or []]


def run_cross_check(conn: sqlite3.Connection, name: str, fetcher, config: CouncilsConfig, *, allow_large: bool,
                    offline: bool) -> CouncilResult:
    """Fetch + extract (or, offline, replay from cache) the documents of one cross-check source."""
    result = CouncilResult(name)
    refs = cross_check_docs(config, name)
    result.listed = result.selected = len(refs)
    for ref in refs:
        try:
            if offline:
                doc = next((d for d in db.all_source_documents(conn)
                            if d.doc_key == ref.doc_key and not d.superseded_by), None)
                path = fetcher.cached_raw(doc.doc_id, doc.source_url) if doc else None
                if path is None:
                    res = IngestResult(ref, "skipped", detail="offline: raw bytes not in .cache/raw")
                else:
                    d, n = extract_document(conn, path, council_id=doc.council_id, url=doc.source_url,
                                            extractor=ref.extractor, doc_type=doc.doc_type, content_type=doc.content_type,
                                            financial_year=doc.financial_year, title=doc.title,
                                            cache_dir=fetcher.cache_dir)
                    res = IngestResult(ref, "extracted", n, detail=f"{d.extract_status} (replayed from cache)")
            else:
                res = fetch_and_extract(fetcher, conn, ref, allow_large=allow_large)
        except Exception as exc:
            log.exception("cross-check %s: %s failed", name, ref.url)
            res = IngestResult(ref, "error", detail=f"{type(exc).__name__}: {exc}")
        result.add(res)
    return result


# ---- the whole run ----------------------------------------------------------------------------
@dataclass
class RunOutput:
    run_id: str
    iso_week: str
    summary: dict
    summary_path: Path
    digest: digest.DigestResult | None
    ui_dir: Path | None


def run_pipeline(conn: sqlite3.Connection, councils: list[str] | None = None, *, fetcher=None,
                 config: CouncilsConfig | None = None, out_dir: Path | str | None = None,
                 data_dir: Path | str | None = DATA_DIR, offline: bool = False, allow_large: bool = False,
                 pdf: bool = True, enable: set[str] | None = None, max_meeting_docs: int | None = None,
                 now: datetime | None = None) -> RunOutput:
    config = config or load_councils()
    enable = set(enable or ())
    keys = councils or [k for k, c in config.councils.items() if c.enabled]
    unknown = [k for k in keys if k not in config.councils]
    if unknown:
        raise ValueError(f"unknown council(s): {', '.join(unknown)}; configured: {', '.join(config.councils)}")
    if fetcher is None:
        from radar.fetch import Fetcher

        fetcher = Fetcher(config=config, conn=conn)
    out = Path(out_dir) if out_dir else OUT_DIR
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(ZoneInfo(TIMEZONE)).date()

    run = changes.start_run(conn, keys, now=now)
    results: list[CouncilResult] = []
    for key in keys:
        council = config.councils[key]
        log.info("council %s", key)
        if offline:
            res = CouncilResult(key)
            replay_council(conn, council, fetcher, config, res)
        else:
            res = run_council(conn, council, fetcher, config, today=today, allow_large=allow_large, enable=enable,
                              max_meeting_docs=max_meeting_docs, data_dir=Path(data_dir) if data_dir else None)
        results.append(res)

    cross: dict[str, CouncilResult] = {}
    if "blackspot" in enable and config.cross_checks.get("blackspot"):
        log.info("cross-check blackspot")
        cross["blackspot"] = run_cross_check(conn, "blackspot", fetcher, config, allow_large=allow_large, offline=offline)
    link_report = dedupe.link(conn)
    tagged = blackspot.tag(conn) if "blackspot" in enable else 0
    change_report = changes.record_changes(conn, run, now=now)
    errors = [{"council": r.council, "error": e} for r in [*results, *cross.values()] for e in r.errors]
    errors += [{"council": "fetcher", "error": f"host {h} paused: {why}"}
               for h, why in sorted(getattr(fetcher, "paused_this_run", {}).items())]
    totals: dict[str, int] = {}
    for r in results:
        for k, v in r.outcomes.items():
            totals[k] = totals.get(k, 0) + v
    counts = {"docs_listed": sum(r.listed for r in results), "docs_selected": sum(r.selected for r in results),
              "mentions_extracted": sum(r.mentions for r in results), **{f"docs_{k}": v for k, v in totals.items()}}
    changes.finish_run(conn, run, counts, errors)

    dg = digest.write_digest(conn, out, run.iso_week, pdf=pdf, now=now, blackspot_enabled="blackspot" in enable)
    ui_dir = export.export_ui(conn, out, now=now, blackspot_enabled="blackspot" in enable)

    per_council_projects = {r[0]: r[1] for r in conn.execute("SELECT council_id, COUNT(*) FROM project GROUP BY council_id")}
    summary = {
        "run_id": run.run_id, "iso_week": run.iso_week, "started_at": run.started_at, "finished_at": run.finished_at,
        "mode": "offline-replay" if offline else "live", "baseline": change_report.baseline,
        "councils": {r.council: {
            "name": config.councils[r.council].name, "docs_listed": r.listed, "docs_selected": r.selected,
            "outcomes": r.outcomes, "mentions_extracted": r.mentions,
            "projects_total": per_council_projects.get(r.council, 0), "documents": r.docs,
            "skipped": r.skipped, "errors": r.errors,
            "status": "ok" if r.outcomes.get("extracted", 0) + r.outcomes.get("not_modified", 0) and not r.errors
            else ("partial" if r.outcomes.get("extracted", 0) + r.outcomes.get("not_modified", 0) else "failed"),
        } for r in results},
        "totals": {**counts, "projects": change_report.projects, "events": len(change_report.events),
                   "events_by_type": change_report.by_type, "linked": dict(link_report.linked), "possible_duplicates_new": link_report.duplicates_new},
        "cross_checks": {k: {
            "name": (config.cross_checks.get(k) or {}).get("name"), "docs_listed": r.listed, "outcomes": r.outcomes,
            "mentions_extracted": r.mentions, "projects_tagged": tagged, "documents": r.docs, "errors": r.errors,
        } for k, r in cross.items()},
        "paused_hosts": dict(getattr(fetcher, "paused_this_run", {})),
        "errors": errors,
        "outputs": {"digest_html": dg.html_path.name, "digest_pdf": dg.pdf_path.name if dg.pdf_path else None,
                    "pdf_engine": dg.pdf_engine, "pdf_error": dg.pdf_error, "ui": "ui/index.html"},
    }
    out.mkdir(parents=True, exist_ok=True)
    summary_path = out / f"run-summary-{run.iso_week}.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    return RunOutput(run.run_id, run.iso_week, summary, summary_path, dg, ui_dir)
