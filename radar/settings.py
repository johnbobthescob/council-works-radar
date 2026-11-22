"""Central settings: paths, compliance constants (PLAN.md §9) and config loaders.

Values here are the defaults from PLAN.md §3.4 and §9. Per-council overrides
live in config/councils.yaml under `defaults`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
COUNCILS_YAML = CONFIG_DIR / "councils.yaml"
TRADES_YAML = CONFIG_DIR / "trades.yaml"

# Runtime state (gitignored). Created on demand by the modules that use them.
DATA_DIR = PROJECT_ROOT / "data"
CACHE_DIR = PROJECT_ROOT / ".cache"
OUT_DIR = PROJECT_ROOT / "out"
SQLITE_PATH = DATA_DIR / "radar.sqlite"
FETCH_LOG_PATH = DATA_DIR / "fetch_log.jsonl"
HOST_PAUSE_PATH = DATA_DIR / "host_pause.json"

# PLAN.md §9.1: honest, identifiable UA. Never a browser UA; no personal email.
USER_AGENT = (
    "CouncilWorksRadar/0.1 (+https://github.com/johnbobthescob/council-works-radar; "
    "pre-tender research bot; respects robots.txt)"
)

TIMEZONE = "Australia/Perth"

# PLAN.md §9.3: >= 5 s between requests to the same host (floor 2 s, R§3.3 says 5 s).
MIN_DELAY_SECONDS = 5.0
# PLAN.md §9.2: robots.txt cache TTL.
ROBOTS_TTL_SECONDS = 24 * 3600
# PLAN.md §9.3: exponential backoff on 5xx (3 tries).
BACKOFF_SECONDS = (10, 30, 90)
# PLAN.md §9.6: size caps.
MAX_BYTES = 80 * 1024 * 1024          # hard cap
LARGE_THRESHOLD_BYTES = 40 * 1024 * 1024  # opt-in via --allow-large
LISTING_MAX_BYTES = 10 * 1024 * 1024  # listing / HTML responses
# PLAN.md §3.3: meeting selection window and cap.
MEETING_WINDOW_DAYS = 60
MAX_MEETING_DOCS_PER_RUN = 2
# PLAN.md §9.4: pause a host after 403 / 429 / challenge page.
HOST_PAUSE_SECONDS = 24 * 3600
# PLAN.md §9.10: quotes capped at 300 characters.
MAX_QUOTE_CHARS = 300


@dataclass(frozen=True)
class Defaults:
    min_delay_s: float = MIN_DELAY_SECONDS
    max_bytes: int = MAX_BYTES
    large_threshold_bytes: int = LARGE_THRESHOLD_BYTES
    meeting_window_days: int = MEETING_WINDOW_DAYS
    max_meeting_docs_per_run: int = MAX_MEETING_DOCS_PER_RUN


@dataclass
class Adapter:
    type: str
    raw: dict[str, Any]


@dataclass
class Council:
    key: str
    name: str
    enabled: bool
    hosts: list[str]
    blocked_hosts: list[str]
    terms_url: str | None
    adapters: list[Adapter]
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


@dataclass
class Blocklist:
    host: str
    path_prefix: str | None = None
    reason: str | None = None


@dataclass
class CouncilsConfig:
    defaults: Defaults
    councils: dict[str, Council]
    global_blocklist: list[Blocklist]


@dataclass
class Trade:
    code: str
    label: str
    keywords: list[str]
    negative: list[str]
    on_negative: str | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


@dataclass
class TradesConfig:
    trades: dict[str, Trade]
    code_prefixes: dict[str, dict[str, str]]
    context_required: dict[str, list[str]]
    composite_rules: list[dict[str, Any]]


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    return data


def load_councils(path: Path = COUNCILS_YAML) -> CouncilsConfig:
    data = _read_yaml(path)
    defaults = Defaults(**(data.get("defaults") or {}))

    councils: dict[str, Council] = {}
    for key, body in (data.get("councils") or {}).items():
        adapters = [Adapter(type=a["type"], raw=a) for a in body.get("adapters") or []]
        councils[key] = Council(
            key=key,
            name=body["name"],
            enabled=bool(body.get("enabled", False)),
            hosts=list(body.get("hosts") or []),
            blocked_hosts=list(body.get("blocked_hosts") or []),
            terms_url=body.get("terms_url"),
            adapters=adapters,
            raw=body,
        )

    blocklist = [Blocklist(**entry) for entry in data.get("global_blocklist") or []]
    return CouncilsConfig(defaults=defaults, councils=councils, global_blocklist=blocklist)


def load_trades(path: Path = TRADES_YAML) -> TradesConfig:
    data = _read_yaml(path)
    trades: dict[str, Trade] = {}
    for code, body in (data.get("trades") or {}).items():
        body = body or {}
        trades[code] = Trade(
            code=code,
            label=body.get("label", code),
            keywords=list(body.get("keywords") or []),
            negative=list(body.get("negative") or []),
            on_negative=body.get("on_negative"),
            raw=body,
        )
    return TradesConfig(
        trades=trades,
        code_prefixes=data.get("code_prefixes") or {},
        context_required={k: list(v) for k, v in (data.get("context_required") or {}).items()},
        composite_rules=list(data.get("composite_rules") or []),
    )
