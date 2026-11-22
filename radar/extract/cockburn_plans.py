"""Extractor: cockburn_plans (PLAN.md §4.3): City of Cockburn "Project Plans" PDF, page text.

One project per page, with labelled fields (R§1.2.5, §5.1): ``ID: 4C.18``, ``Directorate:``, a
budget table (``Total Budget`` and the current-year budget), start / end dates. Programs such as
"Roads Resurfacing Program" also list road segments as bullets ("Elderberry Dr – South Lake to Berrigan Dr").

Output per page:
* one **parent** mention (``council_project_code`` = the ID) carrying the budget; and
* one mention **per segment bullet** (``council_project_code`` = ``<ID>#<road>|<from>|<to>`` using the
  normalised, abbreviation-expanded names so M12's segment matching sees the same strings), no value
  (``value_missing``): the program total stays on the parent only.

``value_amount`` is the current-year budget when the page has one (``current_year``), else the total
(``budget_total``); ``multi_year`` is flagged when the two differ.
Pages with no ``ID:`` label, or several different IDs (contents / index pages), are skipped. Segment
bullets that spill onto a continuation page without an ID are not picked up.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from radar.dates import parse_project_date
from radar.extract.common import funding_refs, make_mention
from radar.models import ProjectMention, SourceDocument
from radar.normalise import Segment, lookup_suburb, parse_fy, parse_money, parse_segment
from radar.text import DocText

NAME = "cockburn_plans"
VERSION = "1"

_MONEY = r"\$?\s*(?P<amt>[\d][\d,]*(?:\.\d+)?(?:\s*(?:million|m))?)"
_ID_RE = re.compile(r"(?<![\w.])ID\s*:?\s*(?P<id>\d+[A-Z]\.\d+)\b")
_DIRECTORATE_RE = re.compile(r"Directorate\s*:\s*(?P<v>[^\n]+)", re.I)
_TOTAL_RE = re.compile(rf"Total\s+(?:Project\s+)?Budget\s*:?\s*{_MONEY}", re.I)
_FY = r"20\d{2}\s*[-/–]\s*(?:20)?\d{2}"
_CURRENT_RE = re.compile(
    rf"(?:Current\s+Year\s+Budget|{_FY}\s+Budget|Budget\s+(?:for\s+)?(?:FY\s*)?{_FY})\s*:?\s*{_MONEY}", re.I)
_START_RE = re.compile(r"(?:Start|Commencement)\s+Date\s*:?\s*(?P<v>[^\n]+)", re.I)
_END_RE = re.compile(r"(?:End|Completion|Finish)\s+Date\s*:?\s*(?P<v>[^\n]+)", re.I)
_FUNDING_RE = re.compile(r"Funding(?:\s+Sources?)?\s*:\s*(?P<v>[^\n]+)", re.I)
_DESC_RE = re.compile(
    r"(?:Project\s+)?Description\s*:?\s*\n?(?P<v>.+?)(?=\n\s*(?:Locations?|Scope|Budget|Directorate|Total|Start|End|Funding|Streets?)\b|\Z)",
    re.I | re.S)
_BOILERPLATE = re.compile(r"^(?:city\s+of\s+cockburn|project\s+plans?\b.*|page\s+\d+.*|\d{1,3}|"
                          r"(?:directorate|total\s+budget|id)\s*:.*)$", re.I)
_BULLET = re.compile(r"^\s*(?:[•●▪◦·*–—-]|•)\s+")


@dataclass
class PlanSegment:
    seg: Segment
    line: str


@dataclass
class PlanPage:
    page: int
    project_id: str
    title: str
    directorate: str | None = None
    total_budget: float | None = None
    current_budget: float | None = None
    start_raw: str | None = None
    end_raw: str | None = None
    description: str | None = None
    funding_line: str | None = None
    start: int = 0                      # char span of the header block in the page text
    end: int = 0
    segments: list[PlanSegment] = field(default_factory=list)


def _money(m: re.Match | None) -> float | None:
    return parse_money("$" + re.sub(r"\s+", " ", m["amt"]).strip()) if m else None


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def segment_code(project_id: str, seg: Segment) -> str:
    return f"{project_id}#{_slug(seg.road)}|{_slug(seg.from_road)}|{_slug(seg.to_road)}"


def parse_plan_page(page_no: int, text: str) -> PlanPage | None:
    ids = {m["id"] for m in _ID_RE.finditer(text)}
    if len(ids) != 1:
        return None
    id_m = _ID_RE.search(text)
    pid = id_m["id"]

    lines = [(m.start(), m.group(0)) for m in re.finditer(r"[^\n]+", text)]
    title, title_pos = "", id_m.start()
    id_line_idx = next(i for i, (pos, ln) in enumerate(lines) if pos <= id_m.start() < pos + len(ln) + 1)
    for pos, ln in reversed(lines[:id_line_idx]):
        if ln.strip() and not _BOILERPLATE.match(ln.strip()):
            title, title_pos = ln.strip(), pos
            break
    if not title:
        for pos, ln in lines[id_line_idx + 1:]:
            if ln.strip() and not re.match(r"^\s*[A-Za-z ]{2,30}\s*:", ln) and not _BOILERPLATE.match(ln.strip()):
                title, title_pos = ln.strip(), id_m.start()
                break
    if not title:
        return None

    page = PlanPage(page_no, pid, title)
    ends = [id_m.end()]

    def grab(rx):
        m = rx.search(text)
        if m:
            ends.append(m.end())
        return m

    d = grab(_DIRECTORATE_RE)
    page.directorate = d["v"].strip() if d else None
    page.total_budget = _money(grab(_TOTAL_RE))
    page.current_budget = _money(grab(_CURRENT_RE))
    s, e = grab(_START_RE), grab(_END_RE)
    page.start_raw, page.end_raw = (s["v"].strip() if s else None), (e["v"].strip() if e else None)
    f = _FUNDING_RE.search(text)
    page.funding_line = f["v"].strip() if f else None
    ds = _DESC_RE.search(text)
    page.description = " ".join(ds["v"].split()) if ds else None
    page.start, page.end = min(title_pos, id_m.start()), max(ends)

    seen: set[tuple[str, str, str]] = set()
    for _pos, ln in lines:
        if not _BULLET.match(ln):
            continue
        body = _BULLET.sub("", ln).strip()
        seg = parse_segment(body)
        if not seg or seg.raw.rstrip(".;, ") != body.rstrip(".;, ") or seg.key in seen:
            continue
        seen.add(seg.key)
        page.segments.append(PlanSegment(seg, body))
    return page


def extract(doc: DocText, source: SourceDocument, *, now: str | None = None) -> list[ProjectMention]:
    mentions: list[ProjectMention] = []
    for pt in doc.pages:
        plan = parse_plan_page(pt.page, pt.text)
        if not plan:
            continue
        fy = source.financial_year or parse_fy(pt.text[:400]) or parse_fy(doc.pages[0].text[:400])
        date_start = parse_project_date(plan.start_raw)
        date_end = parse_project_date(plan.end_raw, end=True)
        date_stated = " – ".join(x for x in (plan.start_raw, plan.end_raw) if x) or None
        context = " ".join(x for x in (plan.description,) if x)

        flags: list[str] = []
        if plan.current_budget is not None:
            value, basis = plan.current_budget, "current_year"
            if plan.total_budget is not None and plan.total_budget != plan.current_budget:
                flags.append("multi_year")
        elif plan.total_budget is not None:
            value, basis = plan.total_budget, "budget_total"
        else:
            value, basis = None, None
            flags.append("value_missing")
        suburb = lookup_suburb(f"{plan.title} {plan.description or ''}", "cockburn")
        if not suburb and not plan.segments:
            flags.append("name_only_no_location")
        refs = funding_refs(plan.funding_line or plan.description or "")
        common = dict(page=plan.page, stage="adopted_budget", financial_year=fy, date_stated=date_stated,
                      date_start=date_start, date_end=date_end, now=now)

        mentions.append(make_mention(
            source, extractor=NAME, version=VERSION, anchor=plan.project_id, project_name_raw=plan.title,
            quote=" ".join(pt.text[plan.start:plan.end].split()), char_start=plan.start, char_end=plan.end,
            description=context or None, council_project_code=plan.project_id, suburb=suburb,
            value_amount=value, value_basis=basis, value_gst="unknown", funding_refs=refs, flags=flags, **common))

        for item in plan.segments:
            seg = item.seg
            code = segment_code(plan.project_id, seg)
            at = pt.text.find(item.line, plan.end)
            at = at if at >= 0 else pt.text.find(item.line)
            mentions.append(make_mention(
                source, extractor=NAME, version=VERSION, anchor=code, project_name_raw=item.line, quote=item.line,
                char_start=at, char_end=at + len(item.line), description=" ".join((plan.title, context)).strip(),
                council_project_code=code, location_raw=item.line, road_name=seg.road, from_road=seg.from_road,
                to_road=seg.to_road, direction=seg.direction,
                suburb=lookup_suburb(item.line, "cockburn"), value_amount=None, value_basis=None,
                funding_refs=refs, flags=["value_missing"], **common))
    return mentions
