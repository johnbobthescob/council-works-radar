"""Command-line entry point: `python -m radar <subcommand>`.

Stages (PLAN §2.1, §12.2): list, extract (fetch + extract), link, digest, export/ui, and run (all stages);
plus robots-check, trades-report, serve, prune and config-check. Heavy imports happen inside each handler.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from radar import __version__
from radar.settings import load_councils, load_trades


def _config_check(_args: argparse.Namespace) -> int:
    councils = load_councils()
    trades = load_trades()
    enabled = [c for c in councils.councils.values() if c.enabled]
    print(f"councils: {len(councils.councils)} configured, {len(enabled)} enabled")
    print(f"global blocklist entries: {len(councils.global_blocklist)}")
    print(f"trade labels: {len(trades.trades)}")
    return 0


def robots_targets(councils) -> list[tuple[str, str]]:
    """(label, url) pairs to check: every configured council host, plus blocklist entries (PLAN §9.2)."""
    targets: list[tuple[str, str]] = []
    seen: set[str] = set()
    for council in councils.councils.values():
        for host in council.hosts:
            url = f"https://{host}/"
            if url not in seen:
                seen.add(url)
                targets.append((council.key, url))
    for entry in councils.global_blocklist:
        url = f"https://{entry.host}{entry.path_prefix or '/'}"
        if url not in seen:
            seen.add(url)
            targets.append(("blocklist", url))
    for council in councils.councils.values():
        for host in council.blocked_hosts:
            url = f"https://{host}/"
            if url not in seen:
                seen.add(url)
                targets.append((council.key + " (blocked_hosts)", url))
    return targets


def robots_decision(fetcher, url: str, offline: bool) -> str:
    from radar.fetch import blocklist_match

    why = blocklist_match(url, fetcher.config.global_blocklist, fetcher._blocked_hosts)
    if why:
        return f"BLOCKED   ({why}; never contacted)"
    if fetcher.pauses.is_paused(url.split("/")[2]):
        return "PAUSED    (host paused after 403/429/challenge)"
    allowed, reason = fetcher.robots.can_fetch(url, offline=offline)
    if reason == "robots_unknown":
        return "UNKNOWN   (offline and no cached robots.txt)"
    return ("ALLOWED   " if allowed else "DISALLOWED") + f" ({reason})"


def _robots_check(args: argparse.Namespace) -> int:
    """Live by default (it fetches each host's robots.txt, rate-limited); --offline uses blocklist + cache only."""
    from radar.fetch import Fetcher

    councils = load_councils()
    fetcher = Fetcher(config=councils)
    targets = [("custom", u) for u in args.url] if args.url else robots_targets(councils)
    width = max(len(label) for label, _ in targets)
    for label, url in targets:
        print(f"{label:<{width}}  {url:<70}  {robots_decision(fetcher, url, args.offline)}")
    return 0


def _prune(args: argparse.Namespace) -> int:
    from radar.fetch import Fetcher

    removed = Fetcher().prune_raw(args.days)
    print(f"pruned {removed} file(s) older than {args.days} days from .cache/raw")
    return 0


def trades_report_text(conn, top: int = 20) -> str:
    """Per-trade mention counts and the most frequent UNKNOWN project names (PLAN §6.5 tuning loop)."""
    import json
    from collections import Counter

    per_trade: Counter[str] = Counter()
    unknown: Counter[str] = Counter()
    total = 0
    for row in conn.execute("SELECT project_name_raw, trade_labels FROM project_mention"):
        total += 1
        codes = {lab["code"] for lab in json.loads(row["trade_labels"] or "[]")} or {"UNKNOWN"}
        per_trade.update(codes)
        if codes == {"UNKNOWN"}:
            unknown[row["project_name_raw"].strip()] += 1
    lines = [f"mentions: {total}"]
    if not total:
        lines.append("no mentions in the database yet (extractors arrive in M8+); use --gold to check the rules")
        return "\n".join(lines)
    lines.append("per-trade counts (a mention can have several labels):")
    lines += [f"  {code:<13} {n}" for code, n in per_trade.most_common()]
    lines.append(f"top {top} UNKNOWN names:")
    lines += [f"  {n:>4}  {name}" for name, n in unknown.most_common(top)] or ["  (none)"]
    return "\n".join(lines)


def gold_report_text(path=None) -> tuple[str, bool]:
    from radar import trades

    rows = trades.load_gold(path) if path else trades.load_gold()
    metrics = trades.evaluate_gold(rows)
    lines = [f"gold rows: {len(rows)}", f"{'label':<13} {'prec':>5} {'recall':>6}  tp fp fn"]
    ok = True
    for code, m in metrics.items():
        flag = "" if m["precision"] >= 0.85 and m["recall"] >= 0.85 else "  <-- below 0.85"
        ok &= not flag
        lines.append(f"{code:<13} {m['precision']:>5.2f} {m['recall']:>6.2f}  {m['tp']:>2} {m['fp']:>2} {m['fn']:>2}{flag}")
    return "\n".join(lines), ok


def _trades_report(args: argparse.Namespace) -> int:
    from radar import db

    if args.gold is not None:
        text, ok = gold_report_text(args.gold or None)
        print(text)
        return 0 if ok else 1
    conn = db.connect(args.db)
    print(trades_report_text(conn, args.top))
    return 0


def format_docrefs(refs, limit: int | None = None) -> str:
    rows = sorted(refs, key=lambda r: r.meeting_date or "", reverse=True)
    if limit:
        rows = rows[:limit]
    lines = []
    for r in rows:
        size = f"{r.size_bytes / 1048576:.1f}MB" if r.size_bytes else "-"
        lines.append(f"{r.meeting_date or r.financial_year or '-':<10}  {r.doc_type:<15} {r.fmt or '-':<5} {size:>8}  "
                     f"{(r.meeting_type or r.title or '')[:40]:<40}  {r.url}")
    return "\n".join(lines)


def _list(args: argparse.Namespace) -> int:
    from radar.adapters import list_council
    from radar.fetch import Fetcher

    councils = load_councils()
    council = councils.councils.get(args.council)
    if council is None:
        print(f"unknown council {args.council!r}; configured: {', '.join(councils.councils)}", file=sys.stderr)
        return 2
    refs = list_council(council, Fetcher(config=councils))
    print(format_docrefs(refs, args.limit) or "(no documents found)")
    return 0


def _extract(args: argparse.Namespace) -> int:
    from radar import db
    from radar.extract import IMPLEMENTED
    from radar.ingest import extract_document, fetch_and_extract

    conn = db.connect(args.db)
    if args.file:
        missing = [n for n in ("council", "extractor", "url") if not getattr(args, n)]
        if missing:
            print(f"--file needs --{', --'.join(missing)}", file=sys.stderr)
            return 2
        doc, n = extract_document(conn, args.file, council_id=args.council, url=args.url, extractor=args.extractor,
                                  doc_type=args.doc_type, content_type="text/html" if str(args.file).lower().endswith(("htm", "html")) else None)
        print(f"{doc.extract_status}: {n} mention(s) from {args.file}")
        return 0
    if not args.council:
        print("give --council X (live: fetches that council's configured documents) or --file PATH", file=sys.stderr)
        return 2
    from radar.adapters import list_council
    from radar.fetch import Fetcher

    councils = load_councils()
    council = councils.councils.get(args.council)
    if council is None:
        print(f"unknown council {args.council!r}; configured: {', '.join(councils.councils)}", file=sys.stderr)
        return 2
    fetcher = Fetcher(config=councils, conn=conn)
    # Annual documents only (static_links); meeting extractors arrive in later milestones.
    council_static = type(council)(**{**council.__dict__, "adapters": [a for a in council.adapters if a.type == "static_links"]})
    refs = [r for r in list_council(council_static, fetcher) if r.extractor in IMPLEMENTED]
    if not refs:
        print("no documents with an implemented extractor")
        return 0
    for ref in refs:
        result = fetch_and_extract(fetcher, conn, ref, allow_large=args.allow_large)
        print(f"{result.outcome:<12} {result.mentions:>4} mention(s)  {ref.url}  {result.detail or ''}")
    return 0


def _link(args: argparse.Namespace) -> int:
    from radar import blackspot, db, dedupe

    conn = db.connect(args.db)
    report = dedupe.link(conn, council_id=args.council, rebuild=args.rebuild)
    print(report.summary())
    tagged = blackspot.tag(conn)
    if tagged:
        print(f"Black Spot cross-check: {tagged} mention(s) linked to council projects")
    return 0


def _digest(args: argparse.Namespace) -> int:
    from radar import db, digest

    conn = db.connect(args.db)
    res = digest.write_digest(conn, args.out, args.week, pdf=not args.no_pdf)
    print(f"wrote {res.html_path}")
    if res.pdf_path:
        print(f"wrote {res.pdf_path} ({res.pdf_engine})")
    elif res.pdf_error:
        print(f"PDF not written: {res.pdf_error}", file=sys.stderr)
    return 0


def _export(args: argparse.Namespace) -> int:
    from radar import db, export

    ui = export.export_ui(db.connect(args.db), args.out)
    print(f"wrote {ui / 'data.js'} (+ index.html, app.js, style.css)")
    return 0


def _serve(args: argparse.Namespace) -> int:
    """Serve out/ on 127.0.0.1 (PLAN §7.2). Bound to localhost only; never exposed on the network."""
    import functools
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path

    from radar.settings import OUT_DIR

    root = Path(args.out) if args.out else OUT_DIR
    if not (root / "ui" / "index.html").exists():
        print(f"{root / 'ui' / 'index.html'} not found; run `python -m radar export` (or `run`) first", file=sys.stderr)
        return 1
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(root))
    with ThreadingHTTPServer(("127.0.0.1", args.port), handler) as srv:
        print(f"serving {root} at http://127.0.0.1:{srv.server_address[1]}/ui/  (Ctrl+C to stop)")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


def _run(args: argparse.Namespace) -> int:
    import logging

    from radar import db, pipeline

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    councils = [c.strip() for c in args.councils.split(",") if c.strip()] if args.councils else None
    conn = db.connect(args.db)
    try:
        res = pipeline.run_pipeline(conn, councils, out_dir=args.out, offline=args.offline, allow_large=args.allow_large,
                                    pdf=not args.no_pdf, enable=set(args.enable or ()), max_meeting_docs=args.max_meeting_docs)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    s = res.summary
    print(f"run {res.run_id} ({s['mode']}, {'baseline' if s['baseline'] else 'incremental'}) week {res.iso_week}")
    for cid, c in s["councils"].items():
        outcomes = ", ".join(f"{k} {v}" for k, v in sorted(c["outcomes"].items())) or "no documents"
        print(f"  {cid:<11} {c['status']:<8} listed {c['docs_listed']:>3}  selected {c['docs_selected']:>2}  [{outcomes}]  "
              f"mentions {c['mentions_extracted']:>4}  projects {c['projects_total']:>4}")
        for e in c["errors"]:
            print(f"      error: {e}")
        for n in c.get("notes", []):
            print(f"      note: {n}")
    t = s["totals"]
    print(f"projects {t['projects']}, events {t['events']} {t['events_by_type']}")
    if res.digest:
        print(f"wrote {res.digest.html_path}")
        if res.digest.pdf_path:
            print(f"wrote {res.digest.pdf_path} ({res.digest.pdf_engine})")
        elif res.digest.pdf_error:
            print(f"PDF not written: {res.digest.pdf_error}", file=sys.stderr)
    print(f"wrote {res.ui_dir / 'index.html'} (+ data.js)")
    print(f"wrote {res.summary_path}")
    ok = any(c["status"] != "failed" for c in s["councils"].values())
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="radar",
        description="WA Council Works Radar: pre-tender capital-works intelligence from public council documents.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    config_parser = sub.add_parser("config-check", help="load config/councils.yaml and config/trades.yaml and summarise them")
    config_parser.set_defaults(handler=_config_check)

    robots = sub.add_parser("robots-check", help="print the robots.txt decision for every configured host [M2]",
                            description="Checks robots.txt for every configured host (live, rate-limited) and shows "
                                        "the compliance blocklist. Blocklisted hosts are never contacted.")
    robots.add_argument("--offline", action="store_true", help="use blocklist and cached robots.txt only; no network")
    robots.add_argument("--url", action="append", default=[], help="check this URL instead of the configured hosts (repeatable)")
    robots.set_defaults(handler=_robots_check)

    tr = sub.add_parser("trades-report", help="per-trade counts and top UNKNOWN project names [M5]",
                        description="Tuning aid for config/trades.yaml (PLAN §6.5). --gold checks the gold CSV instead.")
    tr.add_argument("--db", default=None, help="SQLite path (default data/radar.sqlite)")
    tr.add_argument("--top", type=int, default=20, help="how many UNKNOWN names to list")
    tr.add_argument("--gold", nargs="?", const="", default=None, metavar="CSV",
                    help="print per-label precision/recall on the gold CSV (default tests/fixtures/trade_gold.csv); exit 1 if < 0.85")
    tr.set_defaults(handler=_trades_report)

    lst = sub.add_parser("list", help="list recent source documents for a council [M6]",
                         description="Runs the council's listing adapters (polite, rate-limited, live) and prints DocRefs.")
    lst.add_argument("--council", required=True, help="council key from config/councils.yaml")
    lst.add_argument("--limit", type=int, default=30, help="max rows to print (newest first)")
    lst.set_defaults(handler=_list)

    ex = sub.add_parser("extract", help="extract project mentions from documents into the DB [M8]",
                        description="Live: fetch a council's configured annual documents (static_links) and run their "
                                    "extractor. Offline: --file PATH --council X --extractor E --url URL.")
    ex.add_argument("--council", help="council key from config/councils.yaml")
    ex.add_argument("--db", default=None, help="SQLite path (default data/radar.sqlite)")
    ex.add_argument("--file", help="extract a local file instead of fetching (no network)")
    ex.add_argument("--extractor", help="extractor name for --file (melville_cwp, cockburn_plans, joondalup_budget, agenda_items)")
    ex.add_argument("--url", help="source URL to record for --file")
    ex.add_argument("--doc-type", default="other")
    ex.add_argument("--allow-large", action="store_true", help="allow documents over the 40 MB threshold")
    ex.set_defaults(handler=_extract)

    lk = sub.add_parser("link", help="dedupe mentions into projects (idempotent) [M12]",
                        description="Assigns unlinked mentions to projects with the key ladder (code, segment, "
                                    "intersection, tender; fuzzy matches are flagged, never merged) and re-derives "
                                    "project state. Safe to re-run.")
    lk.add_argument("--db", default=None, help="SQLite path (default data/radar.sqlite)")
    lk.add_argument("--council", default=None, help="only this council's mentions")
    lk.add_argument("--rebuild", action="store_true", help="clear links/projects first and relink everything")
    lk.set_defaults(handler=_link)

    dg = sub.add_parser("digest", help="write the weekly digest to out/ (HTML, and PDF if possible) [M14]",
                        description="Builds out/digest-YYYY-Www.html (+ .pdf via WeasyPrint, else PyMuPDF Story) from the DB.")
    dg.add_argument("--db", default=None, help="SQLite path (default data/radar.sqlite)")
    dg.add_argument("--out", default=None, help="output directory (default out/)")
    dg.add_argument("--week", default=None, help="ISO week, e.g. 2026-W47 (default: week of the latest run)")
    dg.add_argument("--no-pdf", action="store_true", help="HTML only")
    dg.set_defaults(handler=_digest)

    prune = sub.add_parser("prune", help="delete .cache/raw files older than N days [M2]")
    prune.add_argument("--days", type=int, default=30)
    prune.set_defaults(handler=_prune)

    exp = sub.add_parser("export", aliases=["ui"], help="write out/ui/ (data.js + static UI files) from the DB [M15]",
                         description="Writes out/ui/{index.html,app.js,style.css,data.js}. Open index.html directly or use `serve`.")
    exp.add_argument("--db", default=None, help="SQLite path (default data/radar.sqlite)")
    exp.add_argument("--out", default=None, help="output directory (default out/)")
    exp.set_defaults(handler=_export)

    sv = sub.add_parser("serve", help="serve out/ on http://127.0.0.1:8000/ui/ [M15]",
                        description="A localhost-only wrapper around http.server for the web UI.")
    sv.add_argument("--out", default=None, help="directory to serve (default out/)")
    sv.add_argument("--port", type=int, default=8000)
    sv.set_defaults(handler=_serve)

    rn = sub.add_parser("run", help="full pipeline: list, fetch, extract, link, changes, digest, UI [M16]",
                        description="Runs every stage for the selected councils (polite, rate-limited, live) and writes "
                                    "out/digest-YYYY-Www.html/.pdf, out/ui/ and out/run-summary-YYYY-Www.json. "
                                    "--offline re-extracts already-stored documents from .cache/raw with no network.")
    rn.add_argument("--councils", default=None, help="comma-separated council keys (default: all enabled)")
    rn.add_argument("--db", default=None, help="SQLite path (default data/radar.sqlite)")
    rn.add_argument("--out", default=None, help="output directory (default out/)")
    rn.add_argument("--offline", action="store_true", help="no network: replay stored documents from .cache/raw")
    rn.add_argument("--allow-large", action="store_true", help="allow documents over the 40 MB threshold")
    rn.add_argument("--no-pdf", action="store_true", help="digest HTML only")
    rn.add_argument("--enable", action="append", choices=["blackspot"],
                    help="enable an optional extractor (repeatable), e.g. --enable blackspot")
    rn.add_argument("--max-meeting-docs", type=int, default=None, help="override the per-council meeting doc cap (default 2)")
    rn.add_argument("-v", "--verbose", action="store_true", help="log progress")
    rn.set_defaults(handler=_run)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return args.handler(args)
