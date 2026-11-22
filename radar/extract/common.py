"""Shared extractor helpers (PLAN.md §4.2, §4.3): mention construction, confidence, funding keywords."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from radar import trades
from radar.models import ProjectMention, SourceDocument, make_mention_id

SERIOUS_FLAGS = ("value_ambiguous_column", "wrapped_text_join", "ocr_used")
MILD_FLAGS = ("name_only_no_location", "agenda_not_resolution")

# keyword -> funding ref (schema §4.1). Checked in order; each ref is emitted once.
FUNDING_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"black\s*spot[^.;]*\b(?:federal|commonwealth)\b|\b(?:federal|commonwealth)\b[^.;]*black\s*spot", re.I), "BlackSpot_Federal"),
    (re.compile(r"black\s*spot", re.I), "BlackSpot_State"),
    (re.compile(r"roads?\s+to\s+recovery|\bR2R\b", re.I), "R2R"),
    (re.compile(r"\bMRRG\b|main\s+roads\s+regional|regional\s+road\s+group", re.I), "MRRG"),
    (re.compile(r"\bgrant", re.I), "Grant"),
    (re.compile(r"external\s+fund", re.I), "Grant"),
    (re.compile(r"\breserve\b", re.I), "Reserve"),
    (re.compile(r"\bloan\b|borrowing", re.I), "Loan"),
    (re.compile(r"\bmunicipal\b", re.I), "Municipal"),
    (re.compile(r"\bcarry[\s-]*forward|\bcarried\s+forward|\bbrought\s+forward", re.I), "CarryForward"),
    (re.compile(r"\bcontribution", re.I), "Contribution"),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def funding_refs(text: str) -> list[str]:
    out: list[str] = []
    for rx, ref in FUNDING_PATTERNS:
        if rx.search(text) and ref not in out:
            out.append(ref)
    return out


def score_confidence(*, structured: bool, has_code: bool = False, has_location: bool = False,
                     has_value: bool = False, flags: list[str] | tuple[str, ...] = ()) -> float:
    """PLAN §4.2: start 0.5; +0.2 structured; +0.1 code; +0.1 location and value; -0.2 serious; -0.1 mild."""
    score = 0.5
    if structured:
        score += 0.2
    if has_code:
        score += 0.1
    if has_location and has_value:
        score += 0.1
    score -= 0.2 * sum(f in SERIOUS_FLAGS for f in flags)
    score -= 0.1 * sum(f in MILD_FLAGS for f in flags)
    return round(min(1.0, max(0.0, score)), 2)


def make_mention(doc: SourceDocument, *, extractor: str, version: str, anchor: str, page: int,
                 project_name_raw: str, quote: str, char_start: int | None = None, char_end: int | None = None,
                 bbox: list[float] | None = None, description: str | None = None, structured: bool = True,
                 flags: list[str] | None = None, now: str | None = None, **fields) -> ProjectMention:
    """Build a mention with deterministic id, trade labels, provenance and rule-based confidence."""
    flags = list(dict.fromkeys(flags or []))
    mention = ProjectMention(
        mention_id=make_mention_id(doc.doc_id, extractor, anchor), doc_id=doc.doc_id, council_id=doc.council_id,
        page=page, project_name_raw=project_name_raw, quote=quote, char_start=char_start, char_end=char_end,
        bbox=bbox, uncertainty_flags=flags, extractor=extractor, extractor_version=version,
        extracted_at=now or utc_now(), **fields)
    mention.trade_labels = trades.label_mention(mention, description)
    mention.confidence = score_confidence(
        structured=structured, has_code=bool(mention.council_project_code or mention.cost_code or mention.tender_no),
        has_location=bool(mention.road_name or mention.suburb or mention.location_raw),
        has_value=mention.value_amount is not None, flags=flags)
    return mention
