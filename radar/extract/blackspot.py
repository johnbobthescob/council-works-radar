"""Extractor: blackspot (PLAN.md §4.3, M17): Main Roads WA State Black Spot Program (local roads) PDF.

Layout assumed from RESEARCH (the PDF itself has not been fetched): a heading line
``<Road>-<Road> (City of X)`` or ``<Road> SLK a - b (City of X)`` followed by a treatment description and an
approved amount. The parser is deliberately tolerant: the heading may wrap (council bracket on the next line), the
amount may sit on the heading line after the bracket or on any of the following lines before the next heading.

Only rows whose council matches a configured council ``name`` are kept, and the mention's ``council_id`` is that
target council (the *document* belongs to ``mainroads``). These mentions never create projects; ``radar.blackspot``
links them to council-sourced projects (PLAN §3.2 licence caution).
"""

from __future__ import annotations

import re

from radar.extract.common import make_mention
from radar.models import ProjectMention, SourceDocument
from radar.normalise import find_money, lookup_suburb, normalise_road, parse_fy
from radar.settings import load_councils
from radar.text import DocText

NAME = "blackspot"
VERSION = "2"
FUNDING_REF = "BlackSpot_State"
HEADING_RE = re.compile(
    r"^(?P<loc>.+?)(?:\s+\(?SLK\s*(?P<a>[\d.]+)\s*[-–—]\s*(?P<b>[\d.]+)\)?)?\s+"
    r"\((?P<council>City|Town|Shire) of (?P<name>[^)]+)\)", re.IGNORECASE)
_WRAPPED_BRACKET = re.compile(r"^\((?:City|Town|Shire) of ", re.IGNORECASE)
_ROAD_END = re.compile(r"\b(?:road|rd|street|st|drive|dr|avenue|ave|boulevard|blvd|parade|pde|crescent|cres|highway|hwy|"
                       r"place|pl|court|ct|terrace|tce|close|lane|way|parkway|freeway|bypass|esplanade|circuit|loop|"
                       r"grove|link|rise)\.?$", re.IGNORECASE)
_PAIR_SPLIT = re.compile(r"\s*[-–—/&]\s*|\s+and\s+", re.IGNORECASE)
MAX_FOLLOW_LINES = 8


def target_councils() -> dict[str, str]:
    """lower-case council display name ("city of melville") -> council key."""
    return {c.name.lower(): k for k, c in load_councils().councils.items()}


def split_location(loc: str) -> tuple[str, str | None]:
    """('Preston Point Road-Stock Road') -> ('Preston Point Road', 'Stock Road'); a single road -> (road, None)."""
    parts = [p.strip() for p in _PAIR_SPLIT.split(loc.strip()) if p.strip()]
    if len(parts) == 2 and _ROAD_END.search(parts[0]) and _ROAD_END.search(parts[1]):
        return parts[0], parts[1]
    return loc.strip(), None


def _logical_lines(text: str) -> list[tuple[str, int]]:
    """(line, char offset in page text); a bracket-only line wrapped from the heading is joined to its predecessor."""
    out: list[tuple[str, int]] = []
    pos = 0
    for raw in text.split("\n"):
        line = raw.strip()
        if line and _WRAPPED_BRACKET.match(line) and out and not HEADING_RE.match(out[-1][0]):
            out[-1] = (f"{out[-1][0]} {line}", out[-1][1])
        elif line:
            out.append((line, pos + (len(raw) - len(raw.lstrip()))))
        pos += len(raw) + 1
    return out


def extract(doc: DocText, source: SourceDocument, *, now: str | None = None) -> list[ProjectMention]:
    targets = target_councils()
    fy = source.financial_year or parse_fy(doc.full_text[:600])
    mentions: list[ProjectMention] = []
    seen: dict[str, int] = {}
    offset = 0
    for page in doc.pages:
        lines = _logical_lines(page.text)
        heads = [i for i, (ln, _) in enumerate(lines) if HEADING_RE.match(ln)]
        for n, i in enumerate(heads):
            line, start = lines[i]
            m = HEADING_RE.match(line)
            council_name = f"{m['council']} of {m['name'].strip()}"
            council_id = targets.get(council_name.lower())
            if council_id is None:
                continue
            end_line = heads[n + 1] if n + 1 < len(heads) else len(lines)
            body = [line[m.end():].strip()] + [ln for ln, _ in lines[i + 1:min(end_line, i + 1 + MAX_FOLLOW_LINES)]]
            body_text = " ".join(b for b in body if b)
            hits = find_money(body_text)
            amount = hits[0].amount if hits else None
            desc = body_text
            for h in reversed(hits[:1]):
                desc = (body_text[:h.start] + " " + body_text[h.end:])
            desc = re.sub(r"\s+", " ", desc).strip(" -–—:;,") or None
            loc = m["loc"].strip()
            a, b = (float(m["a"]), float(m["b"])) if m["a"] and m["b"] else (None, None)
            if a is not None:
                road, cross = loc, None
                location = f"{loc} SLK {m['a']} - {m['b']}"
            else:
                road, cross = split_location(loc)
                location = f"{road} / {cross}" if cross else loc
            key = normalise_road(location) + f"|{council_id}"
            seen[key] = seen.get(key, 0) + 1
            anchor = key if seen[key] == 1 else f"{key}#{seen[key]}"
            end_line_idx = min(end_line, i + 1 + MAX_FOLLOW_LINES)
            last_text, last_off = lines[end_line_idx - 1]
            flags = [] if amount is not None else ["value_missing"]
            mentions.append(make_mention(
                source, extractor=NAME, version=VERSION, anchor=anchor, page=page.page, project_name_raw=loc,
                quote=" ".join([line] + [ln for ln, _ in lines[i + 1:end_line_idx]]),
                char_start=offset + start, char_end=offset + last_off + len(last_text),
                description=desc, location_raw=location, road_name=road, slk_from=a, slk_to=b,
                suburb=lookup_suburb(desc, council_id), value_amount=amount,
                value_basis="current_year" if amount is not None else None,
                funding_refs=[FUNDING_REF], stage="adopted_budget", financial_year=fy, flags=flags, now=now))
            mentions[-1].council_id = council_id
        offset += len(page.text) + 2           # DocText.full_text joins pages with "\n\f"
    return mentions
