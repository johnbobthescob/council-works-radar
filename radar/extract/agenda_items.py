"""Extractor: agenda_items (PLAN.md §4.3, RESEARCH §5.1 / §5.3): council agenda and minutes items.

Shared by every council's meeting documents, PDF or Infocouncil HTML.

1. **Segment** the document into items with the council's item-number regex (``agenda_items.item_pattern``
   in ``config/councils.yaml``; built-in defaults below): Stirling ``12.1/CR3``, Wanneroo ``AS01-11/26``,
   generic ``12.1`` / ``12.1.3``. An item heading is a line that *starts* with an item number followed by a
   title. Table-of-contents lines (dotted leaders, Infocouncil TOC table rows) are skipped, and when the
   same item number heads several segments the longest one wins (TOC entries are short).
2. **Keep civil items:** title or first 500 characters match the civil keywords (tender, resurfac, road,
   footpath, drainage, black spot, roundabout, capital works, budget amendment, MRRG, Roads to Recovery,
   kerb, line marking). Non-civil tenders are kept too (the trade matcher labels them NON_CIVIL).
3. **Facts:** the item is split into body / officer recommendation / council resolution sections. In each,
   the award sentence gives the tender number (``Tender No. 26173``, ``T26/27-05``), supplier
   (``tender from X for ...``), scope (``for ...``) and value (``$1,064,854 (ex GST)``).
4. **Stage:** award in minutes -> ``tender_awarded``; award in an agenda -> ``tender_advertised`` flagged
   ``agenda_not_resolution``; budget amendment / variation / review -> ``budget_review``; invitation to
   tender -> ``tender_advertised``; deferral in the resolution -> ``deferred``.
5. **In-document dedupe:** recommendation and resolution usually repeat the same award. One mention is kept
   per (item, tender_no, value), preferring resolution > recommendation > body; when a resolution exists,
   recommendation-only facts for the same tender (an amended value) are dropped.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
from functools import lru_cache

from radar.extract.common import funding_refs, make_mention
from radar.models import ProjectMention, SourceDocument
from radar.normalise import (find_money, gst_basis, lookup_suburb, normalise_road, parse_fy, parse_intersection,
                             parse_segment)
from radar.text import DocText

NAME = "agenda_items"
VERSION = "1"

GENERIC_PATTERN = r"\d{1,2}\.\d{1,2}(?:\.\d{1,2})?"
DEFAULT_PATTERNS = {
    "stirling": r"\d{1,2}\.\d{1,2}/[A-Z]{1,5}\d{1,3}",
    "wanneroo": r"[A-Z]{2}\d{2}-\d{2}/\d{2}",
}

CIVIL_RE = re.compile(
    r"\btender|\bresurfac|\broads?\b|\bfootpath|\bdrainage|black\s*spot|\broundabout|capital\s+works|"
    r"budget\s+(?:amendment|variation|review)|\bMRRG\b|roads\s+to\s+recovery|\bkerb|line\s*marking", re.I)
_TOC_LINE = re.compile(r"\.{4,}|…{2,}|_{4,}|\s{2,}\d{1,3}\s*$")

_REC_MARK = re.compile(r"(?im)^[ \t]*(?:responsible\s+)?(?:officer'?s?|officer\s*’s|committee)?\s*recommendations?\b[ \t]*:?")
_RES_MARK = re.compile(r"(?im)^[ \t]*(?:council\s+resolution|resolution\b|committee\s+resolution|moved\b[ \t]*:?[ \t]*cr\b)")

TENDER_NO_RE = re.compile(
    r"\b(?:Tender|RFT|RFQ)\s*(?:No\.?|Number|#)?\s*:?\s*(?P<no>(?:[A-Z]{1,4}\s?)?\d[\d/.-]*\d|\d)(?![\d,])", re.I)
_STOP = r"(?=\s*\(ABN|\s*,|\s*;|\s+for\b|\s+be\s+accepted|\s+is\s+accepted|\s+in\s+the\b|\s+at\s+(?:a|the)\b|\s+as\s+the\b|\s+to\s+the\s+value|\.\s|\.?$)"
SUPPLIER_RES = (
    re.compile(rf"\btenders?\s+(?:(?:No\.?|Number)\s*)?(?:(?:[A-Z]{{1,4}}\s?)?\d[\w/.-]*\s+)?(?:submitted\s+)?(?:from|by)\s+"
               rf"(?P<s>[A-Z0-9][^\n]*?){_STOP}", re.S | re.I),
    re.compile(rf"\baward(?:s|ed)?\s+(?:the\s+)?(?:contract|tender)\b[^.;]{{0,80}}?\bto\s+(?P<s>[A-Z][^\n]*?){_STOP}", re.S),
    re.compile(rf"\bappoints?\s+(?P<s>[A-Z][^\n]*?){_STOP}", re.S),
)
AWARD_RE = re.compile(r"\b(?:accepts?|accepted|award(?:s|ed)?|appoints?)\b", re.I)
_NOT_AWARD = re.compile(r"\b(?:not|declines?(?:\s+to)?|rejects?|decline)\s+(?:to\s+)?(?:accept|award|all)\b", re.I)
SCOPE_RE = re.compile(
    r"\bfor\s+(?:the\s+)?(?P<scope>[^$;\n]+?)(?=\s+(?:for|in|at)\s+(?:a|the)\s+(?:lump\s+sum\s+|total\s+|estimated\s+)?"
    r"(?:amount|sum|price|cost|value|period)|\s+for\s+\$|,?\s+(?:at|for)\s+a\s+(?:total|cost|price)|\s*\$|\s*\(|;|\.\s|$)", re.I)
BUDGET_RE = re.compile(r"budget\s+(?:amendment|variation|review|adjustment)|amend(?:s|ment\s+to)?\s+the\s+(?:\S+\s+)?(?:annual\s+)?budget|"
                       r"mid[-\s]year\s+(?:budget\s+)?review", re.I)
INVITE_RE = re.compile(r"\b(?:invite|call(?:ing)?\s+(?:for\s+)?|advertis\w*)\s+(?:public\s+)?tenders?\b", re.I)
DEFER_RE = re.compile(r"\bdefer(?:s|red)?\b|\blays?\s+(?:the\s+item\s+)?on\s+the\s+table\b", re.I)
DEADLINE_RE = re.compile(r"Tender\s+(?:Deadline|Closing\s+Date|Closed?)\s*:?\s*(?P<v>[^\n]+)", re.I)


@lru_cache(maxsize=None)
def item_pattern_for(council_id: str) -> str:
    """``agenda_items.item_pattern`` from councils.yaml, else the built-in default, else the generic pattern."""
    try:
        from radar.settings import load_councils
        council = load_councils().councils.get(council_id)
        cfg = (council.raw.get("agenda_items") or {}) if council else {}
        if cfg.get("item_pattern"):
            return cfg["item_pattern"]
    except Exception:  # noqa: BLE001 - config problems must not stop extraction; fall back to defaults
        pass
    return DEFAULT_PATTERNS.get(council_id, GENERIC_PATTERN)


# ---- text with page mapping --------------------------------------------------------------------
class Flat:
    """All page texts joined by "\\n"; maps global offsets back to (page, local offset)."""

    def __init__(self, doc: DocText) -> None:
        self.starts: list[int] = []
        self.pages: list[int] = []
        self.lengths: dict[int, int] = {pt.page: len(pt.text) for pt in doc.pages}
        parts, pos = [], 0
        for pt in doc.pages:
            self.starts.append(pos)
            self.pages.append(pt.page)
            parts.append(pt.text)
            pos += len(pt.text) + 1
        self.text = "\n".join(parts)
        self.skip: list[tuple[int, int]] = []
        if doc.kind == "html":     # Infocouncil TOC rows: table rows never start an item
            self.skip = [(b.char_start, b.char_end) for b in doc.blocks if b.kind == "tr"]

    def locate(self, offset: int) -> tuple[int, int]:
        i = max(0, bisect.bisect_right(self.starts, offset) - 1)
        return self.pages[i], offset - self.starts[i]

    def page_len(self, page: int) -> int:
        return self.lengths[page]

    def skipped(self, offset: int) -> bool:
        return any(a <= offset < b for a, b in self.skip)


@dataclass
class Item:
    item_id: str
    title: str
    start: int                  # global offsets into Flat.text
    end: int
    heading_end: int

    def body(self, flat: Flat) -> str:
        return flat.text[self.start:self.end]


def segment_items(doc: DocText, pattern: str) -> tuple[Flat, list[Item]]:
    flat = Flat(doc)
    rx = re.compile(rf"(?m)^[ \t]*(?P<id>{pattern})(?![\w/])[ \t]*[-–—:]?[ \t]*(?P<title>[A-Z0-9][^\n]*)?$")
    heads: list[tuple[int, str, str, int]] = []
    for m in rx.finditer(flat.text):
        line = m.group(0)
        if flat.skipped(m.start()) or _TOC_LINE.search(line):
            continue
        title = (m["title"] or "").strip()
        if not title:      # title on the next non-empty line
            nxt = re.match(r"\s*\n[ \t]*([^\n]+)", flat.text[m.end():])
            title = nxt.group(1).strip() if nxt else ""
        if not title or len(title) > 200:
            continue
        heads.append((m.start(), m["id"], title, m.end()))
    items: list[Item] = []
    for i, (start, iid, title, hend) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(flat.text)
        items.append(Item(iid, title, start, end, hend))
    best: dict[str, Item] = {}
    for it in items:
        if it.item_id not in best or (it.end - it.start) > (best[it.item_id].end - best[it.item_id].start):
            best[it.item_id] = it
    return flat, sorted(best.values(), key=lambda it: it.start)


def is_civil(item: Item, flat: Flat) -> bool:
    return bool(CIVIL_RE.search(item.title) or CIVIL_RE.search(flat.text[item.heading_end:item.heading_end + 500]))


# ---- sections and facts ------------------------------------------------------------------------
@dataclass
class Section:
    kind: str                   # body | recommendation | resolution
    start: int                  # global offsets
    end: int


def sections(item: Item, flat: Flat) -> list[Section]:
    text = flat.text[item.heading_end:item.end]
    marks = sorted([(m.start(), "recommendation") for m in _REC_MARK.finditer(text)] +
                   [(m.start(), "resolution") for m in _RES_MARK.finditer(text)])
    out: list[Section] = []
    first = marks[0][0] if marks else len(text)
    out.append(Section("body", item.heading_end, item.heading_end + first))
    for i, (pos, kind) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        if out and out[-1].kind == kind and out[-1].end == item.heading_end + pos:
            out[-1].end = item.heading_end + end          # "Moved Cr" right after "Council Resolution"
            continue
        out.append(Section(kind, item.heading_end + pos, item.heading_end + end))
    return out


@dataclass
class Fact:
    section: str
    start: int                  # global span of the evidence sentence
    end: int
    tender_no: str | None = None
    supplier: str | None = None
    scope: str | None = None
    value: float | None = None
    gst: str = "unknown"
    award: bool = False

    @property
    def key(self) -> tuple[str | None, float | None]:
        return (self.tender_no, self.value)


_RANK = {"resolution": 3, "recommendation": 2, "body": 1}


def _clean(s: str | None) -> str | None:
    if not s:
        return None
    s = " ".join(s.split()).strip(" ,;:-–")
    return s.rstrip(".") if not s.endswith(("Ltd.", "Inc.", "Co.")) else s


def tender_no(text: str) -> str | None:
    m = TENDER_NO_RE.search(text)
    return re.sub(r"\s+", "", m["no"]) if m else None


def _sentence(text: str, pos: int) -> tuple[int, int]:
    """Bounds of the clause around ``pos``: blank line / numbered-clause / sentence boundaries, max ~500 chars."""
    starts = [m.end() for m in re.finditer(r"\n\s*\n|\n\s*(?:\d+|[a-z])[.)]\s|(?<=[.;:])\s+(?=[A-Z0-9])|\n[ \t]*(?=That\b)",
                                           text[:pos])]
    s = starts[-1] if starts else 0
    m = re.compile(r"\n\s*\n|\n\s*(?:\d+|[a-z])[.)]\s|(?<=[a-z0-9)])\.\s+(?=[A-Z])|;\s").search(text, pos)
    e = m.start() + (1 if text[m.start()] == "." else 0) if m else len(text)
    return max(s, pos - 500), min(e, pos + 500)


def facts_in_section(sec: Section, flat: Flat) -> list[Fact]:
    text = flat.text[sec.start:sec.end]
    facts: list[Fact] = []
    taken: list[tuple[int, int]] = []
    for m in AWARD_RE.finditer(text):
        s, e = _sentence(text, m.start())
        clause = text[s:e]
        if any(a <= m.start() < b for a, b in taken):
            continue
        if "tender" not in clause.lower() and not re.search(r"\bcontract\b|\bappoint", clause, re.I):
            continue
        if _NOT_AWARD.search(clause):
            continue
        taken.append((s, e))
        f = Fact(sec.kind, sec.start + s, sec.start + e, award=True, tender_no=tender_no(clause))
        sup_end = 0
        for rx in SUPPLIER_RES:
            sm = rx.search(clause)
            if sm:
                f.supplier, sup_end = _clean(sm["s"]), sm.end()
                break
        sc = SCOPE_RE.search(clause, sup_end)
        if sc:
            f.scope = _clean(sc["scope"])
        _value(f, clause)
        facts.append(f)
    if not facts and sec.kind != "body":
        # no award: a budget amendment / variation amount in the decision text
        bm = BUDGET_RE.search(text)
        hits = find_money(text)
        if bm or hits:
            pos = bm.start() if bm else hits[0].start
            s, e = _sentence(text, pos)
            if hits and not (s <= hits[0].start < e):
                e = min(len(text), max(e, hits[0].end + 40))
            f = Fact(sec.kind, sec.start + s, sec.start + e, tender_no=tender_no(text[s:e]))
            _value(f, text[s:e])
            facts.append(f)
    return facts


def _value(f: Fact, clause: str) -> None:
    hits = find_money(clause)
    if not hits:
        return
    tagged = [h for h in hits if gst_basis(clause[h.end:h.end + 30]) != "unknown"]
    hit = tagged[0] if tagged else hits[0]
    f.value = hit.amount
    f.gst = gst_basis(clause[hit.end:hit.end + 30])


def dedupe_facts(facts: list[Fact]) -> list[Fact]:
    """One fact per (tender_no, value), preferring resolution > recommendation > body (PLAN §4.3)."""
    by_key: dict[tuple, Fact] = {}
    for f in facts:
        cur = by_key.get(f.key)
        if cur is None or _RANK[f.section] > _RANK[cur.section]:
            # keep supplier/scope found in a weaker section if the stronger one lacks them
            if cur is not None:
                f.supplier, f.scope = f.supplier or cur.supplier, f.scope or cur.scope
            by_key[f.key] = f
        else:
            cur.supplier, cur.scope = cur.supplier or f.supplier, cur.scope or f.scope
    kept = list(by_key.values())
    if any(f.section == "resolution" for f in kept):
        res_tenders = {f.tender_no for f in kept if f.section == "resolution"}
        res_awards = any(f.award for f in kept if f.section == "resolution")
        kept = [f for f in kept if f.section == "resolution" or
                not (f.tender_no in res_tenders or (f.award and res_awards and f.tender_no is None))]
    # a fact with no tender number duplicates a numbered one with the same value
    numbered_values = {f.value for f in kept if f.tender_no}
    kept = [f for f in kept if f.tender_no or f.value is None or f.value not in numbered_values]
    # a fact without value or tender duplicates any richer fact
    if len(kept) > 1:
        kept = [f for f in kept if f.tender_no or f.value is not None] or kept[:1]
    return sorted(kept, key=lambda f: f.start)


# ---- mentions ----------------------------------------------------------------------------------
def doc_kind(doc: DocText, source: SourceDocument) -> str:
    if source.doc_type in ("agenda", "minutes"):
        return source.doc_type
    head = f"{source.title or ''} {doc.pages[0].text[:2000] if doc.pages else ''}"
    return "minutes" if re.search(r"\bminutes\b", head, re.I) else "agenda"


def _title_clean(title: str) -> str:
    return " ".join(title.split()).rstrip(" .")


def extract(doc: DocText, source: SourceDocument, *, now: str | None = None,
            item_pattern: str | None = None) -> list[ProjectMention]:
    pattern = item_pattern or item_pattern_for(source.council_id)
    flat, items = segment_items(doc, pattern)
    kind = doc_kind(doc, source)
    fy_doc = source.financial_year
    mentions: list[ProjectMention] = []
    for item in items:
        if not is_civil(item, flat):
            continue
        secs = sections(item, flat)
        facts = dedupe_facts([f for s in secs for f in facts_in_section(s, flat)])
        resolution_text = " ".join(flat.text[s.start:s.end] for s in secs if s.kind == "resolution")
        decision_text = " ".join(flat.text[s.start:s.end] for s in secs if s.kind != "body")
        body_text = item.body(flat)
        title = _title_clean(item.title)
        if not facts:
            facts = [Fact("body", item.start, item.heading_end, tender_no=tender_no(title))]
        deadline = DEADLINE_RE.search(body_text)
        for fact in facts:
            mentions.append(_mention(source, item, fact, flat, kind, title, body_text, decision_text,
                                     resolution_text, deadline["v"].strip() if deadline else None, fy_doc, now))
    return mentions


def _mention(source, item, fact, flat, kind, title, body_text, decision_text, resolution_text, deadline, fy_doc, now):
    flags: list[str] = []
    stage = None
    if resolution_text and DEFER_RE.search(resolution_text):
        stage = "deferred"
    elif fact.award:
        stage = "tender_awarded" if kind == "minutes" else "tender_advertised"
    elif BUDGET_RE.search(title) or BUDGET_RE.search(decision_text):
        stage = "budget_review"
    elif INVITE_RE.search(title) or INVITE_RE.search(decision_text):
        stage = "tender_advertised"
    if kind == "agenda" and stage:
        flags.append("agenda_not_resolution")

    tno = fact.tender_no or tender_no(title)
    loc_text = f"{title}. {fact.scope or ''}"
    seg = parse_segment(title) or parse_segment(fact.scope) or parse_segment(flat.text[fact.start:fact.end])
    pair = None if seg else (parse_intersection(title) or _intersection_in(title))
    loc: dict = {}
    if seg:
        loc = dict(road_name=seg.road, from_road=seg.from_road, to_road=seg.to_road, direction=seg.direction,
                   location_raw=" ".join(seg.raw.split()).rstrip(" ,;."))
    elif pair:
        loc = dict(location_raw=" / ".join(pair))     # dedupe derives the intersection key from location_raw
    else:
        road = _ROAD_ONE.search(title)
        if road:
            loc = dict(road_name=normalise_road(road.group(0)), location_raw=road.group(0))
    evidence = flat.text[fact.start:fact.end]
    suburb = lookup_suburb(_ROAD_ONE.sub(" ", loc_text), source.council_id) or \
        lookup_suburb(_ROAD_ONE.sub(" ", evidence), source.council_id)
    if not loc and not suburb:
        flags.append("name_only_no_location")
    if fact.value is None:
        flags.append("value_missing")
    elif fact.gst == "unknown":
        flags.append("gst_unknown")
    if fact.award:
        basis = "contract_award"
    else:
        basis = "estimate" if fact.value is not None else None

    page, local = flat.locate(fact.start)
    local_end = min(local + max(1, fact.end - fact.start), flat.page_len(page))   # clipped to the first page
    quote = " ".join(flat.text[fact.start:fact.end].split()) or title
    anchor = f"{item.item_id}|{tno or ''}|{fact.value if fact.value is not None else ''}"
    description = " ".join(x for x in (fact.scope,) if x) or None
    refs = funding_refs(f"{title} {decision_text}")
    fy = parse_fy(title) or fy_doc
    return make_mention(
        source, extractor=NAME, version=VERSION, anchor=anchor, page=page, project_name_raw=title, quote=quote,
        char_start=local, char_end=local_end, description=description, structured=False, flags=flags,
        tender_no=tno, supplier_name=fact.supplier, value_amount=fact.value, value_basis=basis, value_gst=fact.gst,
        funding_refs=refs, stage=stage, financial_year=fy, resolution_ref=item.item_id, suburb=suburb,
        date_stated=deadline, now=now, **loc)


_ROAD_TYPES = r"(?:Road|Rd|Street|St|Drive|Dr|Avenue|Ave|Boulevard|Blvd|Parade|Pde|Highway|Hwy|Way|Crescent|Cres|Court|Ct|Place|Pl|Terrace|Tce|Close|Lane)"
_ROAD_ONE = re.compile(rf"\b[A-Z][\w']*(?:\s+[A-Z][\w']*){{0,3}}\s+{_ROAD_TYPES}\b")
_ROAD_PAIR = re.compile(r"([A-Z][\w']*(?:\s+[A-Z][\w']*){0,3}\s+(?:Road|Rd|Street|St|Drive|Dr|Avenue|Ave|Boulevard|Blvd|"
                        r"Parade|Pde|Highway|Hwy|Way|Crescent|Cres))\s*(?:/|&|\band\b)\s*"
                        r"([A-Z][\w']*(?:\s+[A-Z][\w']*){0,3}\s+(?:Road|Rd|Street|St|Drive|Dr|Avenue|Ave|Boulevard|Blvd|"
                        r"Parade|Pde|Highway|Hwy|Way|Crescent|Cres))\b")


def _intersection_in(text: str) -> tuple[str, str] | None:
    """An intersection pair inside a longer title ('Tender 5/26 – Ocean Reef Road / Marmion Avenue Roundabout')."""
    if not re.search(r"intersection|roundabout|black\s*spot|junction|signals", text, re.I):
        return None
    m = _ROAD_PAIR.search(text)
    return parse_intersection(f"{m.group(1)} / {m.group(2)}") if m else None
