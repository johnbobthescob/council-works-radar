"""Document ingest: fetch (politely) -> text layer -> extractor -> SQLite (PLAN.md §2.2, M8).

``extract_document`` works on a local file, so the whole path is testable offline. ``fetch_and_extract``
adds the polite fetcher in front. A 304 reuses the stored document and mentions.
"""

from __future__ import annotations

import hashlib
import inspect
from dataclasses import dataclass
from pathlib import Path

from radar import db, text as textlayer
from radar.adapters.base import DocRef
from radar.extract import get_extractor
from radar.extract.common import utc_now
from radar.models import SourceDocument, canonical_url
from radar.normalise import parse_fy


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_document(conn, path: str | Path, *, council_id: str, url: str, extractor: str, doc_type: str = "other",
                     content_type: str | None = None, financial_year: str | None = None, title: str | None = None,
                     meeting_date: str | None = None, listing_url: str | None = None, http_etag: str | None = None,
                     http_last_modified: str | None = None, cache_dir: Path | None = None) -> tuple[SourceDocument, int]:
    """Run ``extractor`` over a cached raw document; store the source_document row and its mentions."""
    path = Path(path)
    extract = get_extractor(extractor)
    doc_id = _sha256_file(path)
    dt = textlayer.get_text(path, doc_id, content_type, cache_dir=cache_dir)
    fy = financial_year or parse_fy(dt.full_text[:600]) or parse_fy(url)

    key = canonical_url(url)
    prior = db.latest_document_for_key(conn, key)
    existing = db.get_source_document(conn, doc_id)
    if existing is None and prior and not prior.superseded_by and prior.text_sha256 == dt.text_sha256:
        # New bytes but identical text (per-request tokens in HTML, re-saved PDFs): the same version, not a new one.
        existing = prior
    now = utc_now()
    doc = existing or SourceDocument(
        doc_id=doc_id, doc_key=key, council_id=council_id, source_url=url, doc_type=doc_type, title=title,
        meeting_date=meeting_date, financial_year=fy, listing_url=listing_url, retrieved_at=now,
        http_etag=http_etag, http_last_modified=http_last_modified, content_type=content_type,
        bytes=path.stat().st_size, page_count=dt.page_count, text_sha256=dt.text_sha256,
        version_no=(prior.version_no + 1) if prior else 1)
    doc.last_checked_at = now

    kwargs = {"path": path} if "path" in inspect.signature(extract).parameters else {}
    mentions = extract(dt, doc, **kwargs) if dt.status != "failed" else []
    doc.extractor_version = ",".join(sorted({m.extractor_version or "" for m in mentions})) or None
    if dt.status == "ocr_needed":
        doc.extract_status = "ocr_needed"
    elif dt.status == "failed":
        doc.extract_status = "failed"
    else:
        doc.extract_status = "ok" if mentions else "partial"
    if prior and prior.doc_id != doc_id and not existing:
        prior.superseded_by = doc_id
        db.upsert_source_document(conn, prior)
    db.upsert_source_document(conn, doc)
    db.upsert_mentions(conn, mentions)
    return doc, len(mentions)


@dataclass
class IngestResult:
    ref: DocRef
    outcome: str                 # extracted | not_modified | skipped | unsupported | error
    mentions: int = 0
    detail: str | None = None


def fetch_and_extract(fetcher, conn, ref: DocRef, *, allow_large: bool = False) -> IngestResult:
    if not ref.extractor:
        return IngestResult(ref, "unsupported", detail="no extractor configured")
    try:
        get_extractor(ref.extractor)
    except KeyError as exc:
        return IngestResult(ref, "unsupported", detail=str(exc))
    prior = db.latest_document_for_key(conn, ref.doc_key)
    res = fetcher.fetch(ref.url, prior=prior, allow_large=allow_large)
    if res.outcome == "not_modified" and prior:
        db.upsert_source_document(conn, _touched(prior))
        return IngestResult(ref, "not_modified", detail="304; stored mentions kept")
    if not res.ok or not res.path:
        return IngestResult(ref, "skipped" if res.outcome != "error" else "error", detail=f"{res.outcome}: {res.reason}")
    doc, n = extract_document(
        conn, res.path, council_id=ref.council_id, url=ref.url, extractor=ref.extractor, doc_type=ref.doc_type,
        content_type=res.content_type or ("text/html" if ref.fmt == "html" else None), financial_year=ref.financial_year,
        title=ref.title, meeting_date=ref.meeting_date, listing_url=ref.listing_url, http_etag=res.etag,
        http_last_modified=res.last_modified, cache_dir=fetcher.cache_dir)
    return IngestResult(ref, "extracted", n, detail=f"{doc.extract_status}")


def _touched(doc: SourceDocument) -> SourceDocument:
    doc.last_checked_at = utc_now()
    return doc
