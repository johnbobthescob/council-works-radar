"""Command-line entry point: `python -m radar <subcommand>`.

Subcommands are registered now so `--help` documents the planned surface (PLAN §2.3, §10).
Each one that is not yet built exits with status 2 and names the milestone that delivers it.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from radar import __version__
from radar.settings import load_councils, load_trades

NOT_IMPLEMENTED_EXIT = 2

# (name, milestone, help). Milestones refer to PLAN.md §10.
STUB_COMMANDS: list[tuple[str, str, str]] = [
    ("robots-check", "M2", "print the robots.txt decision for every configured host"),
    ("list", "M6", "list recent source documents for a council (--council X)"),
    ("extract", "M8", "extract project mentions from fetched documents"),
    ("trades-report", "M5", "per-trade counts and top UNKNOWN project names"),
    ("link", "M12", "dedupe mentions into projects (idempotent)"),
    ("digest", "M14", "write the weekly digest to out/ (HTML, and PDF if WeasyPrint is installed)"),
    ("export", "M15", "write out/ui/data.js for the web UI"),
    ("serve", "M15", "serve out/ui/ on localhost with http.server"),
    ("run", "M16", "full pipeline for the selected councils (--councils a,b,c)"),
    ("prune", "M2", "delete .cache/raw files older than 30 days"),
]


def _config_check(_args: argparse.Namespace) -> int:
    councils = load_councils()
    trades = load_trades()
    enabled = [c for c in councils.councils.values() if c.enabled]
    print(f"councils: {len(councils.councils)} configured, {len(enabled)} enabled")
    print(f"global blocklist entries: {len(councils.global_blocklist)}")
    print(f"trade labels: {len(trades.trades)}")
    return 0


def _make_stub(name: str, milestone: str):
    def _handler(_args: argparse.Namespace) -> int:
        print(
            f"radar {name}: not implemented yet (planned for milestone {milestone}; see PLAN.md §10)",
            file=sys.stderr,
        )
        return NOT_IMPLEMENTED_EXIT

    return _handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="radar",
        description="WA Council Works Radar: pre-tender capital-works intelligence from public council documents.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    config_parser = sub.add_parser("config-check", help="load config/councils.yaml and config/trades.yaml and summarise them")
    config_parser.set_defaults(handler=_config_check)

    for name, milestone, help_text in STUB_COMMANDS:
        stub = sub.add_parser(name, help=f"{help_text} [{milestone}]", description=help_text)
        stub.set_defaults(handler=_make_stub(name, milestone))

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return args.handler(args)
