"""Extractor: melville_cwp (PLAN.md §4.3): City of Melville Capital Works Program HTML page.

One block (p / li) per project: ``Name ($value) description`` (R§1.2.4). A heading gives the financial
year ("2026/27 Capital Works Program"). Section headings are NOT used for trade matching ("Buildings and
car parks" would label every project beneath it).
A name block whose description sits in the next block is joined with it.

Assumption: the page lists each project's allocation for the program year, so ``value_basis`` is
``current_year``. Black Spot / grant wording in the description fills ``funding_refs``.
"""

from __future__ import annotations

import re

from radar.extract.common import funding_refs, make_mention
from radar.models import ProjectMention, SourceDocument
from radar.normalise import (gst_basis, lookup_suburb, normalise_name, parse_fy, parse_intersection,
                             parse_money, parse_segment)
from radar.text import DocText

NAME = "melville_cwp"
VERSION = "1"
PROJECT_RE = re.compile(
    r"^(?P<name>.+?)\s*\(\s*\$(?P<value>[\d,]+(?:\.\d+)?(?:\s*(?:million|m|k))?)\s*\)\s*(?P<desc>.*)$",
    re.IGNORECASE | re.DOTALL)
_PROJECT_KINDS = ("p", "li", "dd", "dt")


def extract(doc: DocText, source: SourceDocument, *, now: str | None = None) -> list[ProjectMention]:
    blocks = doc.blocks
    fy = source.financial_year
    fy_from_heading = False
    mentions: list[ProjectMention] = []
    seen: dict[str, int] = {}
    skip_next = False
    for i, block in enumerate(blocks):
        if block.kind.startswith("h") and block.kind[1:].isdigit():
            if not fy_from_heading and parse_fy(block.text):     # the first FY in a heading is the page's year
                fy, fy_from_heading = parse_fy(block.text), True
            continue
        if skip_next:
            skip_next = False
            continue
        if block.kind not in _PROJECT_KINDS:
            continue
        m = PROJECT_RE.match(block.text)
        if not m:
            continue
        name, desc = m["name"].strip(" -–—:"), m["desc"].strip()
        end = block.char_end
        if not desc and i + 1 < len(blocks):
            nxt = blocks[i + 1]
            if nxt.kind in _PROJECT_KINDS and not PROJECT_RE.match(nxt.text):
                desc, end, skip_next = nxt.text, nxt.char_end, True
        value = parse_money("$" + re.sub(r"\s+", " ", m["value"]).strip())
        if value is None:
            continue
        full = f"{name}. {desc}" if desc else name
        quote = f"{block.text}" if not skip_next else f"{block.text} {desc}"
        flags = []
        if gst_basis(full) == "unknown":
            flags.append("gst_unknown")
        seg, inter = parse_segment(name), parse_intersection(name)
        suburb = lookup_suburb(full, "melville")
        key = normalise_name(name)
        seen[key] = seen.get(key, 0) + 1
        anchor = key if seen[key] == 1 else f"{key}#{seen[key]}"
        mentions.append(make_mention(
            source, extractor=NAME, version=VERSION, anchor=anchor, page=1, project_name_raw=name, quote=quote,
            char_start=block.char_start, char_end=end,
            description=desc or None,
            location_raw=name if (seg or inter or suburb) else None,
            road_name=seg.road if seg else None, from_road=seg.from_road if seg else None,
            to_road=seg.to_road if seg else None, direction=seg.direction if seg else None, suburb=suburb,
            value_amount=value, value_basis="current_year", value_gst=gst_basis(full),
            funding_refs=funding_refs(desc), stage="adopted_budget", financial_year=fy, flags=flags, now=now))
    return mentions
