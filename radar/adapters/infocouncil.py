"""Listing adapter: infocouncil (PLAN.md §3.1, R§1.2.2).

The portal home page is a server-rendered table. Links look like
``RedirectToDoc.aspx?URL=Open/<YYYY>/<MM>/<MTG>_<DDMMYYYY>_<AGN|MIN>_AT[_SUP][_WEB.htm|.PDF]``.

Selection per (meeting, date, agenda/minutes, supplementary?) group, best first:
  1. the ``.HTM`` paper (``..._AT.HTM``); if only the ``_WEB.htm`` frameset is linked, its paper is
     the same name without ``_WEB`` (R§1.2.2), so we derive it;
  2. an ``_ATT_EXCLUDED`` PDF (attachments split out; much smaller);
  3. the full PDF.
``_BMK`` (bookmark frame) links are ignored.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote

from bs4 import BeautifulSoup

from radar.adapters.base import DocRef, absolute, require_listing
from radar.dates import parse_compact_ddmmyyyy

ADAPTER = "infocouncil"

MEETING_NAMES = {
    "OC": "Ordinary Council", "BSS": "Briefing Session", "PRCCC": "Policy, Risk, Compliance and Community Committee",
    "CPRCC": "Community, Planning and Regulation Committee",
}
_LINK = re.compile(
    r"(?P<prefix>.*?)(?P<path>Open/(?P<y>\d{4})/(?P<m>\d{2})/(?P<code>[A-Za-z]+)_(?P<date>\d{8})_(?P<kind>AGN|MIN)_"
    r"(?P<rest>[A-Za-z_]*?))\.(?P<ext>pdf|htm|html)$", re.IGNORECASE)


def _classify(href: str):
    value = unquote(href)
    m = _LINK.search(value)
    if not m:
        return None
    rest = m["rest"].upper()
    ext = m["ext"].lower()
    if rest.endswith("_BMK"):
        return None
    web = rest.endswith("_WEB")
    if web:
        rest = rest[: -len("_WEB")]
    if ext == "pdf":
        rank, label = (2, "pdf_att_excluded") if "ATT_EXCLUDED" in rest else (3, "pdf")
    elif web:
        rank, label = 1, "html_frameset"
    else:
        rank, label = 0, "html_paper"
    return m, rest, rank, label


def parse_listing(council_id: str, html: str | bytes, page_url: str, *, meeting_codes: list[str] | None = None,
                  prefer: str = "htm_paper") -> list[DocRef]:
    soup = BeautifulSoup(html, "lxml")
    wanted = {c.upper() for c in meeting_codes} if meeting_codes else None
    groups: dict[tuple, list] = {}
    for a in soup.find_all("a", href=True):
        info = _classify(a["href"])
        if not info:
            continue
        m, rest, rank, label = info
        code = m["code"].upper()
        if wanted and code not in wanted:
            continue
        group_key = (code, m["date"], m["kind"].upper(), "_SUP" in rest)
        groups.setdefault(group_key, []).append((rank, label, a["href"], m, rest))

    refs: list[DocRef] = []
    for (code, date, kind, sup), cands in groups.items():
        cands.sort(key=lambda c: c[0])
        if prefer == "pdf":
            cands.sort(key=lambda c: (c[1] not in ("pdf_att_excluded", "pdf"), c[0]))
        rank, label, href, m, rest = cands[0]
        if label == "html_frameset":
            # The frameset's paper is the same URL without "_WEB" and with an upper-case .HTM extension.
            href = re.sub(r"_WEB\.html?$", ".HTM", href, flags=re.IGNORECASE)
        iso = parse_compact_ddmmyyyy(date)
        doc_type = "agenda" if kind == "AGN" else "minutes"
        name = MEETING_NAMES.get(code, code)
        refs.append(DocRef(
            council_id=council_id, url=absolute(page_url, href), doc_type=doc_type,
            title=f"{name} {doc_type}{' (supplementary)' if sup else ''} {iso}",
            meeting_type=name, meeting_date=iso, listing_url=page_url, version_hint="SUP" if sup else None,
            fmt="pdf" if label.startswith("pdf") else "html", adapter=ADAPTER))
    return sorted(refs, key=lambda r: (r.meeting_date or "", r.doc_type), reverse=True)


def list_docs(council, cfg: dict[str, Any], fetcher) -> list[DocRef]:
    from radar.settings import LISTING_MAX_BYTES

    url = cfg["listing_url"]
    res = fetcher.fetch(url, max_bytes=LISTING_MAX_BYTES)
    require_listing(res, f"listing {url}")
    prefer = "pdf" if cfg.get("prefer") == "pdf" else "htm_paper"
    return parse_listing(council.key, res.read_bytes(), url, meeting_codes=cfg.get("meeting_codes"), prefer=prefer)
