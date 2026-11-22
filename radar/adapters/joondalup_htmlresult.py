"""Listing adapter: joondalup_htmlresult (PLAN.md §3.1, R§1.2.1).

``POST /search/htmlresult`` with the ``SI:"CouncilMeeting"`` JSON body documented in RESEARCH.md.
``values`` must be objects (``[{"value": ...}]``); plain strings give HTTP 400. The response is
``{"htmlResult": "<fragment>", ...}``; results come **oldest first**, so we read the record count
(``recordCountValue``) and then jump to the last page(s).

Where ``recordCountValue`` lives is not pinned down in RESEARCH (a JSON key or inside the fragment);
both are handled. Document URLs always come from the listing: 2026 filenames are free text.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup

from radar.adapters.base import DocRef, absolute, doc_type_from_text, parse_size
from radar.dates import parse_date_text

ADAPTER = "joondalup_htmlresult"
DEFAULT_ENDPOINT = "https://www.joondalup.wa.gov.au/search/htmlresult"
FILES_MARKER = "/files/councilmeetings/"
_OLD_NAME = re.compile(r"CJ(\d{2})(\d{2})(\d{2})_(AGN|MIN|BRF)", re.IGNORECASE)
_COUNT_IN_HTML = re.compile(r"recordCountValue[^0-9]{0,60}(\d+)", re.IGNORECASE)
_PR = (("widgetclassname", "AWCouncilMeetingSmartSearchListing"),
       ("widgettemplatename", "AWCouncilMeetingSmartSearchListing"),
       ("classname", "AWPT.CouncilMeeting"),
       ("nodealiaspath", "/"))


def build_request_body(page: int = 1, page_size: int = 24, search_index: str = "CouncilMeeting") -> dict[str, Any]:
    """The exact body from R§1.2.1."""
    return {"SI": search_index, "OB": "", "Q": "", "PS": page_size, "FS": None, "PG": page,
            "PR": [{"key": k, "values": [{"value": v}]} for k, v in _PR],
            "IncludeFirst": False}


def record_count(payload: dict[str, Any]) -> int | None:
    for k, v in payload.items():
        if k.lower() == "recordcountvalue":
            try:
                return int(v)
            except (TypeError, ValueError):
                return None
    m = _COUNT_IN_HTML.search(str(payload.get("htmlResult") or ""))
    return int(m.group(1)) if m else None


def last_pages(total: int, page_size: int, pages_back: int = 2) -> list[int]:
    """Page numbers to fetch, newest first (results are oldest-first, so the last page is the newest)."""
    last = max(1, math.ceil(total / page_size))
    return [p for p in range(last, max(0, last - pages_back), -1)]


def _date_from_name(name: str) -> str | None:
    m = _OLD_NAME.search(name)
    if m:
        yy, mm, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            from datetime import date
            return date(2000 + yy, mm, dd).isoformat()
        except ValueError:
            return None
    return parse_date_text(name)


def parse_html_result(council_id: str, html: str, base_url: str, listing_url: str | None = None) -> list[DocRef]:
    soup = BeautifulSoup(html or "", "lxml")
    refs: dict[str, DocRef] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if FILES_MARKER not in unquote(href).lower():
            continue
        url = absolute(base_url, href)
        fname = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
        text = " ".join(a.get_text(" ").split())
        item = next((p for p in a.parents if p.name in ("li", "article", "tr", "div")), None)
        heading = item.find(["h2", "h3", "h4", "h5"]) if item else None
        heading_text = " ".join(heading.get_text(" ").split()) if heading else None
        context = f"{fname} {text} {heading_text or ''}"
        kind = _OLD_NAME.search(fname)
        if kind:
            doc_type = {"AGN": "agenda", "MIN": "minutes", "BRF": "agenda"}[kind.group(4).upper()]
        else:
            # "Council Agenda Inc Attachments" is the agenda with attachments included, not an attachments file.
            low_ctx = context.lower()
            doc_type = ("minutes" if "minute" in low_ctx else "agenda" if "agenda" in low_ctx
                        else doc_type_from_text(context))
        low = context.lower()
        meeting_type = ("Briefing Session" if "brief" in low or (kind and kind.group(4).upper() == "BRF")
                        else "Committee" if "committee" in low else "Council")
        size = parse_size(item.get_text(" ")) if item else None
        ref = DocRef(
            council_id=council_id, url=url, doc_type=doc_type, title=heading_text or fname.rsplit(".", 1)[0],
            meeting_type=meeting_type, meeting_date=_date_from_name(fname) or parse_date_text(context),
            listing_url=listing_url or base_url, size_bytes=size, fmt="pdf" if ".pdf" in fname.lower() else None,
            adapter=ADAPTER)
        refs[ref.doc_key] = ref
    return sorted(refs.values(), key=lambda r: r.meeting_date or "", reverse=True)


def list_docs(council, cfg: dict[str, Any], fetcher, pages_back: int = 2) -> list[DocRef]:
    from radar.settings import LISTING_MAX_BYTES

    endpoint = cfg.get("endpoint", DEFAULT_ENDPOINT)
    size = int(cfg.get("page_size", 24))
    index = cfg.get("search_index", "CouncilMeeting")
    listing_url = cfg.get("listing_url", endpoint)

    def call(page: int) -> dict[str, Any] | None:
        res = fetcher.fetch(endpoint, method="POST", json_body=build_request_body(page, size, index),
                            max_bytes=LISTING_MAX_BYTES)
        return json.loads(res.read_bytes()) if res.ok and res.path else None

    first = call(1)
    if first is None:
        return []
    total = record_count(first)
    if total is None:
        raise KeyError("recordCountValue not found in the response; endpoint contract changed?")
    refs = parse_html_result(council.key, first.get("htmlResult", ""), endpoint, listing_url) if total <= size else []
    if total > size:
        for page in last_pages(total, size, pages_back):
            payload = call(page)
            if payload:
                refs += parse_html_result(council.key, payload.get("htmlResult", ""), endpoint, listing_url)
    return sorted({r.doc_key: r for r in refs}.values(), key=lambda r: r.meeting_date or "", reverse=True)
