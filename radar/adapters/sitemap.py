"""Listing adapter: sitemap (PLAN.md §3.1, M18) -- the fallback when a council's listing endpoint changes.

Reads ``sitemap_url`` (a ``<urlset>`` or a ``<sitemapindex>``; at most ``max_child_sitemaps`` children are followed),
keeps URLs matching ``pattern`` and derives the meeting date / type / document type from the URL slug. With
``follow_pages: N`` the newest N matching pages are fetched and the agenda/minutes links on them (``doc_pattern``)
become the DocRefs; otherwise the page URL itself is the DocRef (html). Every fetch goes through the polite fetcher.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

from radar.adapters.base import DocRef, doc_type_from_text
from radar.dates import parse_date_flexible, parse_date_text
from radar.settings import TIMEZONE

ADAPTER = "sitemap"
DEFAULT_MAX_CHILD_SITEMAPS = 3
_FORBIDDEN = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_sitemap(xml: bytes | str) -> tuple[list[dict], list[str]]:
    """(url entries ``{"loc", "lastmod"?}``, child sitemap URLs). Raises ValueError on DTDs/entities or bad XML."""
    raw = xml.encode("utf-8") if isinstance(xml, str) else xml
    if _FORBIDDEN.search(raw) or _FORBIDDEN.search(raw.decode("utf-16", "ignore").encode("utf-8", "ignore")
                                                    if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else b""):
        raise ValueError("sitemap contains a DTD or entity declaration; refusing to parse")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError(f"sitemap is not valid XML: {exc}") from exc
    entries: list[dict] = []
    children: list[str] = []
    for node in root:
        kind = _local(node.tag)
        if kind not in ("url", "sitemap"):
            continue
        fields = {_local(c.tag): (c.text or "").strip() for c in node}
        loc = fields.get("loc")
        if not loc:
            continue
        if kind == "sitemap":
            children.append(loc)
        else:
            entry = {"loc": loc}
            if fields.get("lastmod"):
                entry["lastmod"] = fields["lastmod"]
            entries.append(entry)
    return entries, children


def _slug(url: str) -> str:
    return unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])


def _meeting_type(slug: str) -> str | None:
    low = slug.lower()
    if "ordinary" in low:
        return "Ordinary Council Meeting"
    if "special" in low:
        return "Special Council Meeting"
    return None


def entry_to_ref(council_id: str, entry: dict, extractor: str | None = None) -> DocRef:
    slug = _slug(entry["loc"])
    date = parse_date_text(slug.replace("-", " ")) or parse_date_flexible(entry.get("lastmod"))
    return DocRef(council_id=council_id, url=entry["loc"], doc_type=doc_type_from_text(slug), title=slug.replace("-", " "),
                  meeting_type=_meeting_type(slug), meeting_date=date, fmt="html", extractor=extractor, adapter=ADAPTER)


def _fetch_bytes(fetcher, url: str) -> bytes | None:
    from radar.settings import LISTING_MAX_BYTES

    res = fetcher.fetch(url, max_bytes=LISTING_MAX_BYTES)
    return res.read_bytes() if res.ok and res.path else None


def list_docs(council, cfg: dict[str, Any], fetcher) -> list[DocRef]:
    from radar.adapters.static_links import discover_links

    body = _fetch_bytes(fetcher, cfg["sitemap_url"])
    if body is None:
        return []
    entries, children = parse_sitemap(body)
    for child in children[: int(cfg.get("max_child_sitemaps", DEFAULT_MAX_CHILD_SITEMAPS))]:
        sub = _fetch_bytes(fetcher, child)
        if sub is not None:
            entries += parse_sitemap(sub)[0]

    rx = re.compile(cfg["pattern"], re.IGNORECASE) if cfg.get("pattern") else None
    seen: set[str] = set()
    refs: list[DocRef] = []
    for entry in entries:
        if entry["loc"] in seen or (rx and not rx.search(unquote(entry["loc"]))):
            continue
        seen.add(entry["loc"])
        refs.append(entry_to_ref(council.key, entry, cfg.get("extractor")))

    follow = int(cfg.get("follow_pages", 0) or 0)
    if not follow:
        return refs
    # Follow the best recent meeting pages (same preference as the run's selection policy: Ordinary Council,
    # minutes before agendas, newest; never future meetings). Landing pages themselves hold no project text, so
    # pages that are not followed (or yield no links) are listed as ``landing`` refs and are never selected.
    today = _today()
    lo = (today - timedelta(days=int(cfg.get("window_days", 60)))).isoformat()
    recent = [r for r in refs if r.meeting_date and lo <= r.meeting_date <= today.isoformat()]
    recent.sort(key=lambda r: (r.meeting_type != "Ordinary Council Meeting", r.doc_type != "minutes",
                               "".join(chr(0x7f - ord(c)) for c in r.meeting_date or "")))
    to_follow = {r.url for r in recent[:follow]} if cfg.get("doc_pattern") else set()
    out: list[DocRef] = []
    links_seen: set[str] = set()
    for page_ref in sorted(refs, key=lambda r: r.meeting_date or "", reverse=True):
        html = _fetch_bytes(fetcher, page_ref.url) if page_ref.url in to_follow else None
        found = [f for f in (discover_links(council.key, html, page_ref.url, cfg["doc_pattern"]) if html else [])
                 if f.url not in links_seen]
        if not found:
            page_ref.landing = True
            out.append(page_ref)
            continue
        for link in found:
            links_seen.add(link.url)
            link.doc_type = doc_type_from_text(f"{link.title or ''} {unquote(link.url)}", default=page_ref.doc_type)
            link.meeting_date, link.meeting_type = page_ref.meeting_date, page_ref.meeting_type
            link.extractor, link.adapter = cfg.get("extractor"), ADAPTER
            link.fmt = link.fmt or "pdf"
            out.append(link)
    return out


def _today() -> date:
    return datetime.now(ZoneInfo(TIMEZONE)).date()
