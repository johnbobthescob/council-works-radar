"""Listing adapter: cockburn_html (PLAN.md §3.1).

Cockburn's Agendas-and-Minutes page is server-rendered with direct
``/getattachment/<GUID>/ECM_<id>_v<n>_<Title>-pdf.aspx`` links and file sizes. The ECM id and
version drive dedupe/versioning (R§1.2.5).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, unquote, urlsplit

from bs4 import BeautifulSoup

from radar.adapters.base import DocRef, absolute, require_listing, doc_type_from_text, parse_ecm, parse_size
from radar.dates import parse_date_text

ADAPTER = "cockburn_html"
_CONTAINERS = ("li", "tr", "article", "p", "div")


def build_listing_url(listing_url: str, query: dict[str, Any] | None) -> str:
    """``?page=1&keyword=OCM|Ordinary%20Council%20Meeting&titleonly=true&years=2026`` (R§1.2.5)."""
    if not query:
        return listing_url
    parts = []
    for k, v in query.items():
        val = str(v).lower() if isinstance(v, bool) else str(v)
        parts.append(f"{k}={quote(val, safe='|')}")
    return f"{listing_url}?{'&'.join(parts)}"


def _row_text(a) -> str:
    """Text next to the link (file size, date): the nearest ancestor holding only this attachment link."""
    for parent in a.parents:
        if parent.name not in _CONTAINERS:
            continue
        if len(parent.find_all("a", href=lambda h: h and "getattachment" in h.lower())) == 1:
            return " ".join(parent.get_text(" ").split())
    return " ".join(a.get_text(" ").split())


def parse_listing(council_id: str, html: str | bytes, page_url: str, *, meeting_type: str | None = None) -> list[DocRef]:
    soup = BeautifulSoup(html, "lxml")
    refs: dict[str, DocRef] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "getattachment" not in href.lower():
            continue
        url = absolute(page_url, href)
        ecm_id, ecm_v = parse_ecm(url)
        text = " ".join(a.get_text(" ").split())
        fname = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
        title = text or fname
        row = _row_text(a)
        ref = DocRef(
            council_id=council_id, url=url, title=title,
            doc_type=doc_type_from_text(f"{title} {fname}"),
            meeting_type=meeting_type,
            meeting_date=parse_date_text(title) or parse_date_text(fname) or parse_date_text(row),
            listing_url=page_url, ecm_id=ecm_id, ecm_version=ecm_v,
            version_hint=f"ecm:{ecm_id}:v{ecm_v}" if ecm_id else None,
            size_bytes=parse_size(row.replace(title, "")), fmt="pdf" if "pdf" in fname.lower() else None,
            adapter=ADAPTER)
        prev = refs.get(ref.doc_key)
        if prev is None or len(ref.title or "") > len(prev.title or ""):
            refs[ref.doc_key] = ref
    return sorted(refs.values(), key=lambda r: r.meeting_date or "", reverse=True)


def list_docs(council, cfg: dict[str, Any], fetcher) -> list[DocRef]:
    from radar.settings import LISTING_MAX_BYTES

    url = build_listing_url(cfg["listing_url"], cfg.get("query"))
    res = fetcher.fetch(url, max_bytes=LISTING_MAX_BYTES)
    require_listing(res, f"listing {url}")
    return parse_listing(council.key, res.read_bytes(), cfg["listing_url"], meeting_type=cfg.get("meeting_type"))
