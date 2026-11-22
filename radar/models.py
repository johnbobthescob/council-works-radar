"""Data model (PLAN.md §4, §4.1): dataclasses mirroring the SQLite tables.

Fields listed in each class's ``JSON_FIELDS`` are stored as JSON text by ``radar.db``.
IDs are deterministic (sha256 / uuid5) so re-runs are idempotent.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Any, ClassVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Fixed namespace for uuid5 ids (PLAN §4.1 `mention_id`).
RADAR_NAMESPACE = uuid.UUID("6f1c2a4e-5d7b-4c58-9a0e-3b1d2c4f5a60")

DOC_TYPES = (
    "budget", "capital_program", "project_plans", "agenda", "minutes",
    "attachments", "tender_register", "blackspot_list", "other",
)
EXTRACT_STATUSES = ("ok", "partial", "failed", "ocr_needed", "skipped_size")
VALUE_BASES = ("budget_total", "current_year", "municipal", "grant", "contract_award", "estimate")
STAGES = (
    "proposed_budget", "adopted_budget", "budget_review", "tender_advertised",
    "tender_awarded", "in_progress", "completed", "deferred", "deleted",
)
UNCERTAINTY_FLAGS = (
    "value_ambiguous_column", "wrapped_text_join", "name_only_no_location", "multi_year",
    "carry_forward", "possible_duplicate", "ocr_used", "ocr_needed", "value_missing",
    "gst_unknown", "agenda_not_resolution",
)

# Query parameters that are cache-busters, not identity (PLAN §5.1).
_STRIP_QUERY_PARAMS = {"v", "language"}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_url(url: str) -> str:
    """`doc_key`: lower-case scheme/host, no fragment, cache-buster params stripped, %20 kept consistent."""
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in _STRIP_QUERY_PARAMS]
    path = parts.path.replace(" ", "%20")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def make_mention_id(doc_id: str, extractor: str, anchor: str) -> str:
    """Deterministic uuid5 of doc_id + extractor + anchor (PLAN §4.1)."""
    return str(uuid.uuid5(RADAR_NAMESPACE, f"{doc_id}|{extractor}|{anchor}"))


def make_project_id(council_id: str, dedupe_key: str) -> str:
    return str(uuid.uuid5(RADAR_NAMESPACE, f"project|{council_id}|{dedupe_key}"))


def make_event_id(run_id: str, project_id: str, event_type: str, mention_id: str | None = None) -> str:
    return str(uuid.uuid5(RADAR_NAMESPACE, f"event|{run_id}|{project_id}|{event_type}|{mention_id or ''}"))


@dataclass
class SourceDocument:
    doc_id: str                      # sha256 of bytes
    doc_key: str                     # canonical URL
    council_id: str
    source_url: str
    doc_type: str = "other"
    version_no: int = 1
    title: str | None = None
    meeting_type: str | None = None
    meeting_date: str | None = None
    financial_year: str | None = None
    listing_url: str | None = None
    retrieved_at: str | None = None
    last_checked_at: str | None = None
    http_etag: str | None = None
    http_last_modified: str | None = None
    content_type: str | None = None
    bytes: int | None = None
    page_count: int | None = None
    text_sha256: str | None = None
    version_hint: str | None = None
    extractor_version: str | None = None
    extract_status: str | None = None
    superseded_by: str | None = None

    JSON_FIELDS: ClassVar[tuple[str, ...]] = ()
    PK: ClassVar[str] = "doc_id"


@dataclass
class ProjectMention:
    mention_id: str
    doc_id: str
    council_id: str
    page: int
    project_name_raw: str
    quote: str = ""
    bbox: list[float] | None = None
    char_start: int | None = None
    char_end: int | None = None
    council_project_code: str | None = None
    cost_code: str | None = None
    tender_no: str | None = None
    location_raw: str | None = None
    road_name: str | None = None
    from_road: str | None = None
    to_road: str | None = None
    direction: str | None = None
    suburb: str | None = None
    ward: str | None = None
    slk_from: float | None = None
    slk_to: float | None = None
    trade_labels: list[dict[str, Any]] = field(default_factory=list)
    value_amount: float | None = None
    value_basis: str | None = None
    value_gst: str = "unknown"
    funding_refs: list[str] = field(default_factory=list)
    funding_split: dict[str, float] | None = None
    stage: str | None = None
    date_stated: str | None = None
    date_start: str | None = None
    date_end: str | None = None
    financial_year: str | None = None
    resolution_ref: str | None = None
    supplier_name: str | None = None
    confidence: float = 0.5
    uncertainty_flags: list[str] = field(default_factory=list)
    extractor: str | None = None
    extractor_version: str | None = None
    extracted_at: str | None = None

    JSON_FIELDS: ClassVar[tuple[str, ...]] = ("bbox", "trade_labels", "funding_refs", "funding_split", "uncertainty_flags")
    PK: ClassVar[str] = "mention_id"

    def __post_init__(self) -> None:
        # PLAN §4: provenance needs a page and at least one of bbox / char span.
        if self.page is None or self.page < 1:
            raise ValueError("page (1-based) is required for provenance")
        if self.bbox is None and (self.char_start is None or self.char_end is None):
            raise ValueError("provenance needs bbox or char_start/char_end")
        if len(self.quote) > 300:
            self.quote = self.quote[:299].rstrip() + "…"


@dataclass
class FetchLogEntry:
    ts: str
    method: str
    url: str
    host: str
    status: int | None = None
    bytes: int | None = None
    etag: str | None = None
    last_modified: str | None = None
    elapsed_ms: int | None = None
    robots_decision: str | None = None
    cache_hit: bool = False
    note: str | None = None
    id: int | None = None

    JSON_FIELDS: ClassVar[tuple[str, ...]] = ()
    PK: ClassVar[str] = "id"


@dataclass
class Project:
    project_id: str
    council_id: str
    dedupe_key: str
    dedupe_key_type: str
    canonical_name: str | None = None
    road_name: str | None = None
    from_road: str | None = None
    to_road: str | None = None
    suburb: str | None = None
    trade_codes: list[str] = field(default_factory=list)
    latest_value: float | None = None
    latest_value_basis: str | None = None
    latest_stage: str | None = None
    financial_year: str | None = None
    first_seen: str | None = None
    last_seen: str | None = None
    confidence: float = 0.5
    flags: list[str] = field(default_factory=list)
    review_needed: bool = False

    JSON_FIELDS: ClassVar[tuple[str, ...]] = ("trade_codes", "flags")
    PK: ClassVar[str] = "project_id"


@dataclass
class ProjectMentionLink:
    project_id: str
    mention_id: str
    link_method: str = "singleton"   # code | segment | intersection | tender | fuzzy | singleton
    link_score: float = 1.0

    JSON_FIELDS: ClassVar[tuple[str, ...]] = ()
    PK: ClassVar[str] = "mention_id"


@dataclass
class ChangeEvent:
    event_id: str
    run_id: str
    project_id: str
    event_type: str
    old: Any = None
    new: Any = None
    mention_id: str | None = None
    created_at: str | None = None

    JSON_FIELDS: ClassVar[tuple[str, ...]] = ("old", "new")
    PK: ClassVar[str] = "event_id"


@dataclass
class Run:
    run_id: str
    started_at: str
    finished_at: str | None = None
    iso_week: str | None = None
    councils: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    errors: list[Any] = field(default_factory=list)

    JSON_FIELDS: ClassVar[tuple[str, ...]] = ("councils", "counts", "errors")
    PK: ClassVar[str] = "run_id"
