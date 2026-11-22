"""Shared adapter types (PLAN.md §3.1): ``DocRef`` and small parsing helpers.

An adapter turns a listing (config, HTML or JSON) into ``DocRef``s: pointers to documents with the
metadata the listing already tells us. Adapters never download the documents themselves.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from urllib.parse import unquote, urljoin

from radar.models import DOC_TYPES, canonical_url

# Config values that are not model doc_types (councils.yaml uses "capital_works_program").
DOC_TYPE_ALIASES = {"capital_works_program": "capital_program", "project_plan": "project_plans"}

ECM_RE = re.compile(r"ECM_(\d+)_v(\d+)", re.IGNORECASE)
_SIZE_RE = re.compile(r"([\d][\d,]*(?:\.\d+)?)\s*(bytes|kb|mb|gb|b)\b", re.IGNORECASE)
_SIZE_MULT = {"b": 1, "bytes": 1, "kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3}


class ListingError(RuntimeError):
    """A listing request failed (HTTP error, robots/blocklist/pause stop, network). Recorded as an adapter error."""


def require_listing(res, what: str):
    """Return ``res`` if the listing fetch succeeded with a body, else raise ``ListingError``."""
    if not res.ok or not res.path:
        status = f" HTTP {res.status}" if res.status else ""
        raise ListingError(f"{what}: {res.outcome}{status}" + (f" ({res.reason})" if res.reason else ""))
    return res


@dataclass
class DocRef:
    council_id: str
    url: str
    doc_type: str = "other"
    title: str | None = None
    meeting_type: str | None = None
    meeting_date: str | None = None          # ISO
    financial_year: str | None = None
    listing_url: str | None = None
    version_hint: str | None = None          # e.g. "ecm:12915695:v3", "SUP"
    ecm_id: str | None = None
    ecm_version: int | None = None
    size_bytes: int | None = None
    fmt: str | None = None                   # pdf | html
    extractor: str | None = None             # extractor name configured for this document
    adapter: str | None = None
    landing: bool = False                    # a meeting landing page, not a document (sitemap fallback)

    def __post_init__(self) -> None:
        self.doc_type = normalise_doc_type(self.doc_type)
        if self.fmt is None:
            self.fmt = guess_format(self.url)

    @property
    def doc_key(self) -> str:
        return canonical_url(self.url)

    def to_dict(self) -> dict:
        return asdict(self)


def normalise_doc_type(value: str | None) -> str:
    v = DOC_TYPE_ALIASES.get((value or "other").strip().lower(), (value or "other").strip().lower())
    return v if v in DOC_TYPES else "other"


def guess_format(url: str) -> str | None:
    low = unquote(url).lower().split("?")[0]
    if low.endswith((".htm", ".html")):
        return "html"
    if low.endswith(("pdf", "pdf.aspx")) or ".pdf" in low:
        return "pdf"
    return None


def parse_ecm(text: str | None) -> tuple[str | None, int | None]:
    """('ECM_12915695_v3_Ordinary-...') -> ('12915695', 3)."""
    m = ECM_RE.search(unquote(text or ""))
    return (m.group(1), int(m.group(2))) if m else (None, None)


def parse_size(value: object) -> int | None:
    """Bytes from an int, a digit string, or text such as '31.3 MB' / '(PDF 418 KB)'."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if re.fullmatch(r"\d+", s):
        return int(s)
    m = _SIZE_RE.search(s)
    if not m:
        return None
    return int(float(m.group(1).replace(",", "")) * _SIZE_MULT[m.group(2).lower()])


def doc_type_from_text(text: str | None, default: str = "other") -> str:
    """Document type from a title or filename token (minutes beat agenda: 'Minutes ... Agenda Items')."""
    low = (text or "").lower()
    if "minute" in low:
        return "minutes"
    if "attachment" in low or "_att" in low or "report attachments" in low:
        return "attachments"
    if "agenda" in low or re.search(r"(^|[^a-z])agn([^a-z]|$)", low):
        return "agenda"
    return default


def absolute(base: str, href: str) -> str:
    return urljoin(base, href.strip())
