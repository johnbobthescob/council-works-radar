"""Extractor: joondalup_budget (PLAN.md §4.3, RESEARCH §1.2.1 / §5.1): City of Joondalup capital works schedule.

The budget PDF's schedule has one row per project under the header::

    Project Number | Cost Code | Team | Project Name | Description | Suburb | Ward |
    Municipal | Reserve | Trust | Grants New | Grants Cfwd | Contribution | Brought Fwd | Loan | Expenditure

Descriptions (and sometimes names) wrap over 2-4 lines **above and below** the numeric row.

Primary path: **word-box table parser** (PyMuPDF words)
1. Find the header by its label words (``Expenditure`` plus nearby label vocabulary). Labels may be stacked
   over two lines ("Grants / New") or inline; column x-extents come from the label positions, never from
   fixed coordinates. A page without a header reuses the last header seen (continuation pages).
2. Rows are anchored on a ``^[A-Z]{3}\\d{4}$`` project number with a ``W\\d{4}`` cost code on the same line.
3. Numbers on the anchor line are assigned to the numeric column whose right edge (or centre) is nearest:
   ``-`` = 0, ``(1,234)`` = -1234.
4. Text words are assigned to text columns by x-range, grouped into per-column paragraphs by vertical gap,
   and each paragraph is attached to the anchor it vertically contains. Lines taken from above the anchor
   set ``wrapped_text_join``. Section headings (start in the project-number column) and total lines
   (numbers but no anchor) are ignored.

Fallback: **text-layer lines** when no header can be found. The layout text comes from ``pdftotext -layout``
when the source file is available, else it is rebuilt from the word boxes, else the page text is used.
Cells are split on 2+ spaces; the trailing 9 numeric cells are the funding columns + Expenditure.
Every fallback row is flagged ``value_ambiguous_column``.

Both paths flag ``value_ambiguous_column`` when the funding columns don't sum to Expenditure, and
``carry_forward`` when Brought Fwd / Grants Cfwd are non-zero. ``value_basis = budget_total``.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from radar.extract.common import funding_refs as keyword_funding_refs, make_mention
from radar.models import ProjectMention, SourceDocument
from radar.normalise import extract_direction, looks_like_road, lookup_suburb, normalise_road, parse_fy, parse_money, parse_segment
from radar.text import DocText, Word, pdftotext_pages

NAME = "joondalup_budget"
VERSION = "2"
COUNCIL = "joondalup"

CODE_RE = re.compile(r"^[A-Z]{3}\d{4}$")
COST_RE = re.compile(r"^W\d{4}$")
MONEY_CELL_RE = re.compile(r"^(?:\(?-?\$?\d{1,3}(?:,\d{3})*(?:\.\d+)?\)?|-|–|—)$")
WARDS = ("North-Central", "North", "Central", "South-West", "South-East", "South")
_WARD_RE = re.compile(r"^(?:north[\s-]*central|north|central|south[\s-]*west|south[\s-]*east|south)$", re.I)

TEXT_COLS = ("project_number", "cost_code", "team", "project_name", "description", "suburb", "ward")
FUNDING_COLS = ("municipal", "reserve", "trust", "grants_new", "grants_cfwd", "contribution", "brought_fwd", "loan")
NUMERIC_COLS = FUNDING_COLS + ("expenditure",)
GATHER_COLS = ("project_name", "description", "suburb", "ward")

# funding_split key -> funding_refs value (schema §4.1). Trust has no schema equivalent.
SPLIT_REFS = {"municipal": "Municipal", "reserve": "Reserve", "grants_new": "Grant", "grants_cfwd": "Grant",
              "contribution": "Contribution", "brought_fwd": "CarryForward", "loan": "Loan"}

_HEADER_VOCAB = {"project", "number", "no", "no.", "cost", "code", "team", "name", "description", "suburb", "ward",
                 "municipal", "reserve", "reserves", "trust", "grants", "grant", "new", "cfwd", "c/fwd", "carried",
                 "contribution", "contributions", "brought", "fwd", "forward", "loan", "loans", "expenditure", "total"}


def classify_label(label: str) -> str | None:
    """Header label text (any word order, lower or mixed case) -> column key."""
    s = label.lower()
    if "expenditure" in s:
        return "expenditure"
    if "municipal" in s:
        return "municipal"
    if "reserve" in s:
        return "reserve"
    if "trust" in s:
        return "trust"
    if "contribution" in s:
        return "contribution"
    if "loan" in s:
        return "loan"
    if "brought" in s:
        return "brought_fwd"
    if "grant" in s:
        return "grants_cfwd" if re.search(r"c/?fwd|carried|forward", s) else "grants_new"
    if "cost" in s or re.search(r"\bcode\b", s):
        return "cost_code"
    if "team" in s:
        return "team"
    if "desc" in s:
        return "description"
    if "suburb" in s:
        return "suburb"
    if "ward" in s:
        return "ward"
    if "name" in s:
        return "project_name"
    if re.search(r"\bnumber\b|\bno\b\.?", s):
        return "project_number"
    return None


# ---- shared row model -------------------------------------------------------------------------
@dataclass
class BudgetRow:
    page: int
    code: str
    cost_code: str | None = None
    team: str | None = None
    name: str = ""
    description: str = ""
    suburb: str | None = None
    ward: str | None = None
    values: dict[str, float] = field(default_factory=dict)      # numeric column -> amount
    flags: list[str] = field(default_factory=list)
    bbox: list[float] | None = None
    char_start: int | None = None
    char_end: int | None = None
    method: str = "wordbox"                                     # wordbox | layout

    @property
    def expenditure(self) -> float | None:
        return self.values.get("expenditure")

    @property
    def funding_split(self) -> dict[str, float]:
        return {k: self.values[k] for k in FUNDING_COLS if k in self.values}

    def check_sum(self) -> None:
        split = self.funding_split
        exp = self.expenditure
        if exp is None or not split or abs(sum(split.values()) - exp) > 1.0:
            self._flag("value_ambiguous_column")
        if split.get("brought_fwd") or split.get("grants_cfwd"):
            self._flag("carry_forward")

    def _flag(self, f: str) -> None:
        if f not in self.flags:
            self.flags.append(f)


def _join_lines(parts: list[str]) -> str:
    out = ""
    for p in parts:
        p = " ".join(p.split())
        if not p:
            continue
        if out.endswith("-") and p[:1].islower():
            out = out[:-1] + p
        else:
            out = f"{out} {p}" if out else p
    return out


def _is_money(s: str) -> bool:
    return bool(MONEY_CELL_RE.match(s.strip()))


# ---- word-box table parser -------------------------------------------------------------------
@dataclass
class Column:
    key: str
    x0: float
    x1: float

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass
class Header:
    columns: dict[str, Column]
    bottom: float                 # y of the header's lower edge (rows start below)
    height: float                 # typical word height on the header page
    top: float = 0.0              # y of the header's upper edge (a second table on the same page starts here)

    @property
    def ordered(self) -> list[Column]:
        return sorted(self.columns.values(), key=lambda c: c.x0)

    @property
    def numeric(self) -> list[Column]:
        return [c for c in self.ordered if c.key in NUMERIC_COLS]

    @property
    def text(self) -> list[Column]:
        return [c for c in self.ordered if c.key in TEXT_COLS]


def _yc(w: Word) -> float:
    return (w.y0 + w.y1) / 2


def _median_height(words: list[Word]) -> float:
    hs = [w.y1 - w.y0 for w in words if w.y1 > w.y0]
    return statistics.median(hs) if hs else 10.0


def _visual_lines(words: list[Word], tol: float) -> list[list[Word]]:
    """Cluster words into visual lines by vertical centre; each line sorted left to right."""
    lines: list[list[Word]] = []
    for w in sorted(words, key=_yc):
        if lines and abs(_yc(w) - statistics.fmean(_yc(x) for x in lines[-1])) <= tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    return [sorted(line, key=lambda w: w.x0) for line in lines]


def detect_headers(words: list[Word]) -> list[Header]:
    """Every table header on a page, top to bottom (some pages carry two tables with different columns)."""
    out: list[Header] = []
    after = float("-inf")
    while (hd := detect_header(words, after_y=after)) is not None:
        out.append(hd)
        after = hd.bottom
    return out


def detect_header(words: list[Word], *, after_y: float = float("-inf")) -> Header | None:
    """Locate the column header by its label words. Needs Expenditure and a name/description column."""
    h = _median_height(words)
    refs = [w for w in words if w.text.strip(":").lower() == "expenditure" and w.y0 > after_y]
    for ref in sorted(refs, key=_yc):
        band = [w for w in words if abs(_yc(w) - _yc(ref)) <= 2.2 * h and w.text.strip(":").lower() in _HEADER_VOCAB]
        # inline phrases per visual line (word gap < 0.8 h), then stack phrases that overlap in x
        phrases: list[list[Word]] = []
        for line in _visual_lines(band, 0.5 * h):
            cur: list[Word] = []
            for w in line:
                if cur and w.x0 - cur[-1].x1 > 0.8 * h:
                    phrases.append(cur)
                    cur = []
                cur.append(w)
            if cur:
                phrases.append(cur)
        groups: list[list[Word]] = []
        for ph in sorted(phrases, key=lambda p: min(w.x0 for w in p)):
            x0, x1 = min(w.x0 for w in ph), max(w.x1 for w in ph)
            for g in groups:
                gx0, gx1 = min(w.x0 for w in g), max(w.x1 for w in g)
                if x0 < gx1 - 1 and x1 > gx0 + 1:
                    g.extend(ph)
                    break
            else:
                groups.append(list(ph))
        cols: dict[str, Column] = {}
        for g in groups:
            g.sort(key=lambda w: (round(_yc(w) / (0.5 * h)), w.x0))
            key = classify_label(" ".join(w.text for w in g))
            if key and key not in cols:
                cols[key] = Column(key, min(w.x0 for w in g), max(w.x1 for w in g))
        if "expenditure" in cols and ({"project_name", "description"} & set(cols)) and \
                any(k in cols for k in FUNDING_COLS):
            labelled = [w for g in groups for w in g if classify_label(" ".join(x.text for x in g))]
            return Header(cols, max(w.y1 for w in labelled), h, top=min(w.y0 for w in labelled))
    return None


def _text_column(header: Header, x0: float, tol: float) -> str | None:
    texts = header.text
    numeric = header.numeric
    first_num = numeric[0].x0 if numeric else float("inf")
    if x0 >= first_num - tol:
        return None
    ordered = header.ordered
    for i, col in enumerate(ordered):
        if col.key not in TEXT_COLS:
            continue
        nxt = ordered[i + 1].x0 if i + 1 < len(ordered) else float("inf")
        lo = col.x0 - tol if col is not texts[0] else float("-inf")
        if lo <= x0 < nxt - tol:
            return col.key
    return None


def _numeric_column(header: Header, w: Word, tol: float) -> str | None:
    numeric = header.numeric
    if not numeric or w.x1 < numeric[0].x0 - tol:
        return None
    xc = (w.x0 + w.x1) / 2
    best = min(numeric, key=lambda c: min(abs(w.x1 - c.x1), abs(xc - c.xc)))
    return best.key


def refine_text_columns(header: Header, lines: list[list[Word]], h: float) -> Header:
    """Move text-column left edges to where the body text actually starts.

    Header labels are often centred over their column (the real Joondalup schedule centres "Description" ~60 pt
    right of where the descriptions start), so label x0 is not the column edge. Body columns show up as recurring
    *segment starts* (a line's first word, or a word after a wide gap). For each gathered text column, take the
    most frequent segment start in (previous label's x0, this label's x0]."""
    starts: Counter[int] = Counter()
    for line in lines:
        prev = None
        for w in line:
            if prev is None or w.x0 - prev.x1 > 0.8 * h:
                starts[round(w.x0)] += 1
            prev = w
    ordered = header.ordered
    cols = dict(header.columns)
    for i, col in enumerate(ordered):
        if col.key not in GATHER_COLS or i == 0:
            continue
        lo, hi = ordered[i - 1].x0 + 0.3 * h, col.x0 + 0.3 * h
        cands = [(n, x) for x, n in starts.items() if lo < x <= hi and n >= 2]
        if cands:
            _n, x = max(cands)
            cols[col.key] = Column(col.key, float(x), col.x1)
    return Header(cols, header.bottom, header.height)


def parse_wordbox_page(page_no: int, words: list[Word], header: Header) -> list[BudgetRow]:
    h = _median_height(words) or header.height
    tol = 0.3 * h
    body = [w for w in words if w.y0 >= header.bottom - 0.1 * h]
    lines = _visual_lines(body, 0.45 * h)
    header = refine_text_columns(header, lines, h)

    # anchors: project number with a cost code on the same line
    anchors: list[tuple[float, list[Word], Word]] = []
    pn_col = header.columns.get("project_number")
    for line in lines:
        for i, w in enumerate(line):
            if CODE_RE.match(w.text) and any(COST_RE.match(x.text) for x in line[i + 1:i + 4]):
                if pn_col is None or abs(w.x0 - pn_col.x0) < 3 * h or pn_col.x0 - tol <= w.x0 <= pn_col.x1 + 2 * h:
                    anchors.append((statistics.fmean(_yc(x) for x in line), line, w))
                    break
    if not anchors:
        return []
    anchor_ys = [a[0] for a in anchors]

    # classify the remaining lines; headings and total rows contribute no text
    usable: list[Word] = []
    anchor_line_ids = {id(a[1]) for a in anchors}
    first_text = header.text[0] if header.text else None
    second_text_x0 = header.text[1].x0 if len(header.text) > 1 else float("inf")
    for line in lines:
        if id(line) in anchor_line_ids:
            usable.extend(line)
            continue
        has_numbers = any(_is_money(w.text) and _numeric_column(header, w, tol) for w in line)
        is_heading = first_text is not None and line[0].x0 < second_text_x0 - tol and not CODE_RE.match(line[0].text)
        if has_numbers or is_heading:
            continue
        usable.extend(line)

    rows: list[BudgetRow] = []
    for ay, line, anchor in anchors:
        row = BudgetRow(page_no, anchor.text)
        cost = next((x for x in line if COST_RE.match(x.text)), None)
        row.cost_code = cost.text if cost else None
        team_words = [x for x in line if _text_column(header, x.x0, tol) == "team" and x is not cost]
        if team_words:
            row.team = " ".join(x.text for x in team_words)
        nums = [x for x in line if _is_money(x.text) and _numeric_column(header, x, tol)]
        for x in nums:
            key = _numeric_column(header, x, tol)
            amount = parse_money(x.text)
            if key and amount is not None and key not in row.values:
                row.values[key] = amount
        row.bbox = [min(x.x0 for x in line), min(x.y0 for x in line), max(x.x1 for x in line), max(x.y1 for x in line)]
        rows.append(row)

    # gather wrapped text per column: paragraphs by vertical gap, attached to the anchor they contain
    for key in GATHER_COLS:
        col_words = [w for w in usable if not (_is_money(w.text) and _numeric_column(header, w, tol))
                     and _text_column(header, w.x0, tol) == key]
        col_lines = _visual_lines(col_words, 0.45 * h)
        paras: list[list[list[Word]]] = []
        for cl in col_lines:
            if paras and _yc(cl[0]) - _yc(paras[-1][-1][0]) > 1.25 * h:
                paras.append([cl])
            elif paras:
                paras[-1].append(cl)
            else:
                paras.append([cl])
        parts: dict[int, list[tuple[float, str]]] = {}
        for para in paras:
            top, bot = min(w.y0 for cl in para for w in cl), max(w.y1 for cl in para for w in cl)
            inside = [i for i, y in enumerate(anchor_ys) if top - 0.5 * h <= y <= bot + 0.5 * h]
            for cl in para:
                y = _yc(cl[0])
                if len(inside) == 1:
                    idx = inside[0]
                elif len(inside) > 1:
                    idx = min(inside, key=lambda i: abs(anchor_ys[i] - y))
                else:
                    idx = min(range(len(anchor_ys)), key=lambda i: abs(anchor_ys[i] - y))
                    if min(abs(anchor_ys[idx] - top), abs(anchor_ys[idx] - bot)) > 2.5 * h:
                        continue
                parts.setdefault(idx, []).append((y, " ".join(w.text for w in cl)))
                row = rows[idx]
                if y < anchor_ys[idx] - 0.5 * h and key in ("project_name", "description"):
                    row._flag("wrapped_text_join")
                for w in cl:
                    b = row.bbox
                    row.bbox = [min(b[0], w.x0), min(b[1], w.y0), max(b[2], w.x1), max(b[3], w.y1)]
        for idx, items in parts.items():
            text = _join_lines([t for _y, t in sorted(items)])
            row = rows[idx]
            if key == "project_name":
                row.name = text
            elif key == "description":
                row.description = text
            elif key == "suburb":
                row.suburb = text or None
            elif key == "ward":
                row.ward = text or None

    for row in rows:
        row.bbox = [round(v, 2) for v in row.bbox]
        if "expenditure" not in row.values:
            row._flag("value_ambiguous_column")
        row.check_sum()
    return rows


# ---- text-layer (layout lines) fallback ------------------------------------------------------
_CELL_RE = re.compile(r"\S+(?: \S+)*")


def _cells(line: str) -> list[tuple[int, str]]:
    return [(m.start(), m.group(0)) for m in _CELL_RE.finditer(line)]


def words_to_layout(words: list[Word]) -> str:
    """Rebuild ``pdftotext -layout``-style lines from word boxes (characters placed by x position)."""
    if not words:
        return ""
    widths = [(w.x1 - w.x0) / len(w.text) for w in words if w.text and w.x1 > w.x0]
    cw = statistics.median(widths) if widths else 5.0
    out = []
    for line in _visual_lines(words, 0.45 * _median_height(words)):
        buf = ""
        for w in line:
            col = int(round(w.x0 / cw))
            if len(buf) >= col:
                buf += " "
            else:
                buf += " " * (col - len(buf))
            buf += w.text
        out.append(buf.rstrip())
    return "\n".join(out)


def _split_anchor_line(line: str) -> tuple[list[tuple[int, str]], list[str]] | None:
    """(text cells with offsets after code/cost/team, trailing numeric cells) for an anchor line."""
    cells = _cells(line)
    toks: list[tuple[int, str]] = []
    for off, c in cells:          # split each cell into single-space tokens for the code / number ends
        pos = off
        for t in c.split(" "):
            toks.append((pos, t))
            pos += len(t) + 1
    if not toks or not CODE_RE.match(toks[0][1]):
        return None
    # trailing numbers
    nums: list[str] = []
    end = len(toks)
    while end > 1 and _is_money(toks[end - 1][1]):
        nums.insert(0, toks[end - 1][1])
        end -= 1
    if not nums:
        return None
    head = toks[:end]
    # rebuild middle cells (re-merge single-space tokens that were one cell)
    lead = 1
    if lead < len(head) and COST_RE.match(head[lead][1]):
        lead += 1
    if lead < len(head) and re.fullmatch(r"\d{2,4}", head[lead][1]):
        lead += 1
    start_off = head[lead][0] if lead < len(head) else None
    end_off = toks[end][0] if end < len(toks) else len(line)
    middle = [] if start_off is None else [(start_off + off, c) for off, c in _cells(line[start_off:end_off])]
    return middle, [t for _o, t in head[:lead]] + ["|"] + nums


def parse_layout_text(page_no: int, text: str) -> list[BudgetRow]:
    lines = text.splitlines()
    anchors: dict[int, BudgetRow] = {}
    col_starts: dict[int, dict[str, int]] = {}
    offsets = [0]
    for ln in lines:
        offsets.append(offsets[-1] + len(ln) + 1)
    for i, ln in enumerate(lines):
        parsed = _split_anchor_line(ln.strip("\f"))
        if not parsed:
            continue
        lead_off = len(ln) - len(ln.lstrip())
        middle, lead_and_nums = parsed
        sep = lead_and_nums.index("|")
        lead, nums = lead_and_nums[:sep], lead_and_nums[sep + 1:]
        row = BudgetRow(page_no, lead[0], method="layout")
        row.cost_code = next((t for t in lead[1:] if COST_RE.match(t)), None)
        row.team = next((t for t in lead[1:] if re.fullmatch(r"\d{2,4}", t)), None)
        amounts = [parse_money(n) for n in nums]
        if len(amounts) >= len(NUMERIC_COLS):
            row.values = dict(zip(NUMERIC_COLS, amounts[-len(NUMERIC_COLS):]))
        else:
            row.values = {"expenditure": amounts[-1]}
        cells = [c for _o, c in middle]
        starts: dict[str, int] = {}
        if cells and _WARD_RE.match(cells[-1]):
            row.ward = cells.pop()
            starts["ward"] = middle[len(cells)][0]
        if cells and len(cells) >= 2 and lookup_suburb(cells[-1], COUNCIL) and \
                lookup_suburb(cells[-1], COUNCIL).lower() == cells[-1].lower():
            row.suburb = cells.pop()
            starts["suburb"] = middle[len(cells)][0]
        if cells:
            row.name = cells[0]
            starts["project_name"] = middle[0][0]
        if len(cells) > 1:
            row.description = " ".join(cells[1:])
            starts["description"] = middle[1][0]
        row.flags.append("value_ambiguous_column")
        row.char_start, row.char_end = offsets[i] + lead_off, offsets[i] + len(ln)
        anchors[i] = row
        col_starts[i] = starts

    if not anchors:
        return []
    anchor_idx = sorted(anchors)
    gathered: dict[int, dict[str, list[tuple[int, str]]]] = {i: {} for i in anchor_idx}
    for j, ln in enumerate(lines):
        if j in anchors or not ln.strip():
            continue
        if re.search(r"\b(?:expenditure|municipal|project number)\b", ln, re.I) or re.match(r"^\s*(?:sub\s*-?\s*)?total\b", ln, re.I):
            continue
        cells = _cells(ln)
        if cells and _is_money(cells[-1][1].split(" ")[-1]):
            continue                                            # a total / numeric line without an anchor
        near = min(anchor_idx, key=lambda a: (abs(a - j), a < j))
        if abs(near - j) > 3:
            continue
        starts = col_starts[near]
        if not starts:
            continue
        name_start = starts.get("project_name", 0)
        for off, c in cells:
            if off < name_start - 2:
                break                                           # a heading in the code columns
            key = min((k for k, s in starts.items() if s <= off + 2), key=lambda k: off - starts[k], default=None)
            if key in ("project_name", "description"):
                gathered[near].setdefault(key, []).append((j, c))
                if j < near:
                    anchors[near]._flag("wrapped_text_join")
    for i, cols in gathered.items():
        row = anchors[i]
        for key, items in cols.items():
            before = [c for j, c in items if j < i]
            after = [c for j, c in items if j > i]
            current = row.name if key == "project_name" else row.description
            joined = _join_lines(before + [current] + after)
            if key == "project_name":
                row.name = joined
            else:
                row.description = joined
        lo, hi = min([i] + [j for items in cols.values() for j, _ in items]), max([i] + [j for items in cols.values() for j, _ in items])
        row.char_start, row.char_end = offsets[lo], offsets[hi] + len(lines[hi])
    rows = [anchors[i] for i in anchor_idx]
    for row in rows:
        row.check_sum()
    return rows


# ---- mentions ---------------------------------------------------------------------------------
_DESC_SEGMENT = re.compile(r"^\s*(?P<a>[A-Z0-9][\w'./ -]{1,60}?)\s+to\s+(?P<b>[\w'./ -]{2,60}?)\.(?:\s|$)")


def _location(row: BudgetRow) -> dict:
    out: dict = {}
    seg = parse_segment(row.name)
    if not seg and looks_like_road(row.name) and row.description:
        # the real schedule puts the extent in the description: "Glengarry Drive to Doveridge Drive. Resurfacing..."
        m = _DESC_SEGMENT.match(row.description)
        if m:
            base, _direction = extract_direction(row.name)
            seg = parse_segment(f"{base} – {m['a']} to {m['b']}")
    if seg:
        out.update(road_name=seg.road, from_road=seg.from_road, to_road=seg.to_road, direction=seg.direction)
    elif looks_like_road(row.name):
        base, direction = extract_direction(row.name)
        out.update(road_name=normalise_road(base), direction=direction)
    return out


def _quote(row: BudgetRow) -> str:
    parts = [row.code, row.name, row.description, row.suburb]
    q = " – ".join(p for p in parts if p)
    if row.expenditure is not None:
        q += f" – ${row.expenditure:,.0f}"
    return q


def row_to_mention(row: BudgetRow, source: SourceDocument, fy: str | None, now: str | None) -> ProjectMention:
    split = row.funding_split
    refs = [SPLIT_REFS[k] for k in FUNDING_COLS if split.get(k) and k in SPLIT_REFS]
    for r in keyword_funding_refs(f"{row.name} {row.description}"):
        refs.append(r)
    if split.get("grants_cfwd"):
        refs.append("CarryForward")
    refs = list(dict.fromkeys(refs))
    flags = list(row.flags)
    suburb = row.suburb or lookup_suburb(f"{row.name} {row.description}", COUNCIL)
    loc = _location(row)
    if not suburb and not loc:
        flags.append("name_only_no_location")
    if row.expenditure is None:
        flags.append("value_missing")
    location_raw = ", ".join(x for x in (suburb, f"{row.ward} Ward" if row.ward else None) if x) or None
    span = {"bbox": row.bbox} if row.bbox else {"char_start": row.char_start, "char_end": row.char_end}
    return make_mention(
        source, extractor=NAME, version=VERSION, anchor=f"{row.code}|p{row.page}", page=row.page,
        project_name_raw=row.name or row.code, quote=_quote(row), description=row.description or None,
        council_project_code=row.code, cost_code=row.cost_code, location_raw=location_raw, suburb=suburb,
        ward=row.ward, value_amount=row.expenditure, value_basis="budget_total" if row.expenditure is not None else None,
        value_gst="unknown", funding_refs=refs, funding_split=split or None, stage="adopted_budget",
        financial_year=fy, flags=flags, now=now, **span, **loc)


def parse_document(doc: DocText, *, path: str | Path | None = None) -> list[BudgetRow]:
    rows: list[BudgetRow] = []
    header: Header | None = None
    layout_pages: list[str] | None = None
    layout_tried = False
    for pt in doc.pages:
        page_rows: list[BudgetRow] = []
        if pt.words:
            found = detect_headers(pt.words)
            bands: list[tuple[Header, list[Word]]] = []
            if header is not None and (not found or any(w.y1 < found[0].top - 2 for w in pt.words)):
                cont = Header(header.columns, bottom=0.0, height=header.height)        # continuation of last table
                bands.append((cont, [w for w in pt.words if not found or w.y1 <= found[0].top]))
            for i, hd in enumerate(found):
                end = found[i + 1].top if i + 1 < len(found) else float("inf")
                bands.append((hd, [w for w in pt.words if hd.top <= w.y0 < end]))
            if found:
                header = found[-1]
            for hd, band_words in bands:
                page_rows.extend(parse_wordbox_page(pt.page, band_words, hd))
        if not page_rows and (header is None or not pt.words):
            has_anchor = re.search(r"(?m)(?:^|\s)[A-Z]{3}\d{4}\s+W\d{4}\b", pt.text) or \
                any(CODE_RE.match(w.text) for w in pt.words)
            if not has_anchor:
                continue
            text = None
            if path is not None:
                if not layout_tried:
                    layout_pages, layout_tried = pdftotext_pages(Path(path)), True
                if layout_pages and len(layout_pages) >= pt.page:
                    text = layout_pages[pt.page - 1]
            if text is None:
                text = words_to_layout(pt.words) if pt.words else pt.text
            page_rows = parse_layout_text(pt.page, text)
            if text is not pt.text:
                for r in page_rows:       # char spans index a layout text we don't store; keep a bbox instead
                    r.char_start, r.char_end = 0, len(pt.text)
        rows.extend(page_rows)
    return rows


def extract(doc: DocText, source: SourceDocument, *, now: str | None = None,
            path: str | Path | None = None) -> list[ProjectMention]:
    fy = source.financial_year or (parse_fy(doc.pages[0].text[:600]) if doc.pages else None)
    mentions: list[ProjectMention] = []
    seen: set[str] = set()
    for row in parse_document(doc, path=path):
        m = row_to_mention(row, source, fy, now)
        if m.mention_id in seen:
            continue
        seen.add(m.mention_id)
        mentions.append(m)
    return mentions
