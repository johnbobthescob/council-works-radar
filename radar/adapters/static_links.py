"""Listing adapter: static_links (PLAN.md §3.1).

Known document URLs from ``councils.yaml`` plus an optional discover-from-page mode: fetch one index
page and keep links whose URL or text matches ``discover_pattern`` (e.g. Cockburn Strategies-and-Plans).
A discovered link with the same ECM id but a higher version supersedes the configured one.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup

from radar.adapters.base import DocRef, absolute, parse_ecm
from radar.normalise import parse_fy
import re

ADAPTER = "static_links"


def docs_from_config(council_id: str, cfg: dict[str, Any]) -> list[DocRef]:
    refs = []
    for d in cfg.get("docs") or []:
        ecm_id, ecm_v = parse_ecm(d["url"])
        refs.append(DocRef(
            council_id=council_id, url=d["url"], doc_type=d.get("doc_type", "other"), title=d.get("title"),
            financial_year=d.get("financial_year"), listing_url=None, ecm_id=ecm_id, ecm_version=ecm_v,
            version_hint=f"ecm:{ecm_id}:v{ecm_v}" if ecm_id else None, fmt=d.get("format"),
            extractor=d.get("extractor"), adapter=ADAPTER))
    return refs


def discover_links(council_id: str, html: str | bytes, page_url: str, pattern: str, *, doc_type: str = "other",
                   extractor: str | None = None) -> list[DocRef]:
    """Links on ``page_url`` whose (decoded) href or link text matches ``pattern`` (regex, case-insensitive)."""
    rx = re.compile(pattern, re.IGNORECASE)
    soup = BeautifulSoup(html, "lxml")
    refs: list[DocRef] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "javascript:")):
            continue
        text = " ".join(a.get_text(" ").split())
        url = absolute(page_url, href)
        if not rx.search(unquote(url)) and not rx.search(text):
            continue
        ecm_id, ecm_v = parse_ecm(url)
        fy = parse_fy(f"{text} {unquote(urlsplit(url).path)}")
        refs.append(DocRef(
            council_id=council_id, url=url, doc_type=doc_type, title=text or None, financial_year=fy,
            listing_url=page_url, ecm_id=ecm_id, ecm_version=ecm_v,
            version_hint=f"ecm:{ecm_id}:v{ecm_v}" if ecm_id else None, extractor=extractor, adapter=ADAPTER))
    return refs


def merge_refs(refs: list[DocRef]) -> list[DocRef]:
    """One ref per document: same URL, or same ECM id (highest version wins; configured metadata is kept)."""
    out: dict[str, DocRef] = {}
    for ref in refs:
        key = f"ecm:{ref.ecm_id}" if ref.ecm_id else ref.doc_key
        prev = out.get(key)
        if prev is None:
            out[key] = ref
            continue
        newer = (ref.ecm_version or 0) > (prev.ecm_version or 0)
        winner, loser = (ref, prev) if newer else (prev, ref)
        for attr in ("doc_type", "financial_year", "extractor", "title", "fmt"):
            if getattr(winner, attr) in (None, "other"):
                setattr(winner, attr, getattr(loser, attr))
        winner.listing_url = winner.listing_url or loser.listing_url
        out[key] = winner
    return list(out.values())


def list_docs(council, cfg: dict[str, Any], fetcher=None) -> list[DocRef]:
    from radar.settings import LISTING_MAX_BYTES

    refs = docs_from_config(council.key, cfg)
    page = cfg.get("discover_from")
    if page and fetcher is not None:
        res = fetcher.fetch(page, max_bytes=LISTING_MAX_BYTES)
        if res.ok and res.path:
            pattern = cfg.get("discover_pattern")
            if pattern:
                refs += discover_links(council.key, res.read_bytes(), page, pattern,
                                       doc_type=cfg.get("discover_doc_type", "other"),
                                       extractor=cfg.get("discover_extractor"))
    return merge_refs(refs)
