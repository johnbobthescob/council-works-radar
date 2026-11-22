"""M9: Joondalup budget schedule extractor (offline).

* A **synthetic PDF** built here with PyMuPDF reproduces the schedule's column layout: stacked header labels,
  right-aligned numbers, descriptions and names wrapped above and below the numeric row, a section heading,
  a total line, a footer and a continuation page without a header. Its rows are synthetic except the two
  documented in RESEARCH §1.2.1 (RPR3542, RDC2032).
* A small **text-layer fixture** (``fixtures/joondalup_budget_rows.txt``, 2 documented rows) exercises the
  layout-text fallback.
"""

from pathlib import Path

import pymupdf
import pytest

from radar import db
from radar.extract import get_extractor, joondalup_budget as jb
from radar.ingest import extract_document
from radar.models import SourceDocument
from radar.text import DocText, PageText, extract_pdf

FIX = Path(__file__).parent / "fixtures" / "joondalup_budget_rows.txt"
URL = "https://www.joondalup.wa.gov.au/awcontent/Web/Documents/Budget,%20Fees%20and%20Financial%20Reports/Budget-2026-27.pdf"
FS = 7
NOW = "2026-11-22T00:00:00+00:00"

# text columns: left x; numeric columns: right edge x
TEXT_X = {"project_number": 20, "cost_code": 72, "team": 112, "project_name": 145, "description": 290,
          "suburb": 470, "ward": 545}
NUM_R = {"municipal": 690, "reserve": 750, "trust": 800, "grants_new": 860, "grants_cfwd": 920,
         "contribution": 990, "brought_fwd": 1045, "loan": 1090, "expenditure": 1165}
HEADER = {"project_number": ["Project", "Number"], "cost_code": ["Cost", "Code"], "team": ["Team"],
          "project_name": ["Project Name"], "description": ["Description"], "suburb": ["Suburb"], "ward": ["Ward"],
          "municipal": ["Municipal"], "reserve": ["Reserve"], "trust": ["Trust"], "grants_new": ["Grants", "New"],
          "grants_cfwd": ["Grants", "Cfwd"], "contribution": ["Contribution"], "brought_fwd": ["Brought", "Fwd"],
          "loan": ["Loan"], "expenditure": ["Expenditure"]}


def _put(page, x, y, text, right=False, size=FS):
    if right:
        x = x - pymupdf.get_text_length(text, fontname="helv", fontsize=size)
    page.insert_text((x, y), text, fontname="helv", fontsize=size)


def _header(page, y=60):
    for key, labels in HEADER.items():
        for i, label in enumerate(labels):
            if key in NUM_R:
                _put(page, NUM_R[key], y + i * 9, label, right=True)
            else:
                _put(page, TEXT_X[key], y + i * 9, label)


def _row(page, y, code, cost, team, name, desc, suburb, ward, nums, name_lines_above=0, desc_lines_above=0):
    """Numbers + code on baseline y; name/description lines spread around it (vertically centred cells)."""
    _put(page, TEXT_X["project_number"], y, code)
    _put(page, TEXT_X["cost_code"], y, cost)
    _put(page, TEXT_X["team"], y, team)
    for key, lines, above in (("project_name", name, name_lines_above), ("description", desc, desc_lines_above)):
        for i, line in enumerate(lines):
            _put(page, TEXT_X[key], y + (i - above) * 10, line)
    _put(page, TEXT_X["suburb"], y, suburb)
    _put(page, TEXT_X["ward"], y, ward)
    for key, val in zip(NUM_R, nums):
        _put(page, NUM_R[key], y, val, right=True)


def build_schedule_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    p1 = doc.new_page(width=1190, height=842)
    _put(p1, 20, 30, "Capital Works Program 2026-27", size=11)
    _header(p1)
    _put(p1, 20, 100, "Road Preservation / Resurfacing Program")
    # documented row (RESEARCH §1.2.1); description wraps one line above and one below the numeric row
    _row(p1, 130, "RPR3542", "W5033", "621", ["Warwick Road (westbound)"],
         ["Resurfacing and", "rehabilitation works to", "road centreline"], "Warwick", "South-East",
         ["135,455", "-", "-", "270,909", "-", "-", "-", "-", "406,364"], desc_lines_above=1)
    # synthetic row: single-line everything
    _row(p1, 160, "RPR3550", "W5041", "621", ["Marmion Avenue (northbound)"], ["Asphalt resurfacing"],
         "Duncraig", "South", ["220,000", "-", "-", "-", "-", "-", "-", "-", "220,000"])
    _put(p1, 20, 190, "Road Construction")
    # documented row; the name wraps over two lines centred on the numeric row
    _row(p1, 225, "RDC2032", "W4530", "621", ["Eddystone Ave - Joondalup", "to Honeybush"],
         ["Road upgrade and", "dual carriageway"], "Joondalup", "North",
         ["509,102", "-", "-", "240,898", "-", "-", "-", "-", "750,000"], name_lines_above=0, desc_lines_above=0)
    # synthetic row with carried-forward money and a 4-line description (2 above, 1 below)
    _row(p1, 280, "DRN2001", "W6001", "622", ["Hepburn Avenue Drainage"],
         ["Upgrade of stormwater", "drainage pits and pipes", "including new soak wells", "and gully"],
         "Greenwood", "South-East", ["50,000", "-", "-", "-", "-", "-", "100,000", "-", "150,000"], desc_lines_above=2)
    _put(p1, TEXT_X["project_name"], 320, "Total Road Construction")
    _put(p1, NUM_R["expenditure"], 320, "1,526,364", right=True)
    _put(p1, 20, 820, "City of Joondalup Budget 2026-27")
    _put(p1, 1150, 820, "41")

    p2 = doc.new_page(width=1190, height=842)     # continuation page: no header row
    _row(p2, 80, "PEP1001", "W7001", "640", ["Penistone Park"], ["Playground equipment", "replacement"],
         "Greenwood", "South-East", ["80,000", "-", "-", "-", "-", "-", "-", "-", "80,000"])
    # synthetic row whose funding columns don't add up to the total
    _row(p2, 110, "RPR3600", "W5100", "621", ["Whitfords Avenue"], ["Resurfacing"], "Kingsley", "South",
         ["100,000", "-", "-", "-", "-", "-", "-", "-", "120,000"])
    doc.save(path)
    doc.close()
    return path


@pytest.fixture
def pdf_path(tmp_path):
    return build_schedule_pdf(tmp_path / "schedule.pdf")


@pytest.fixture
def source():
    return SourceDocument(doc_id="j" * 64, doc_key=URL, council_id="joondalup", source_url=URL, doc_type="budget",
                          financial_year="2026-27")


@pytest.fixture
def rows(pdf_path):
    return {r.code: r for r in jb.parse_document(extract_pdf(pdf_path))}


def test_header_detected_from_label_words(pdf_path):
    dt = extract_pdf(pdf_path)
    header = jb.detect_header(dt.pages[0].words)
    assert header is not None
    assert set(header.columns) == set(jb.TEXT_COLS) | set(jb.NUMERIC_COLS)
    assert jb.detect_header(dt.pages[1].words) is None        # continuation page reuses page 1's header


def test_codes_rows_and_order(rows):
    assert list(rows) == ["RPR3542", "RPR3550", "RDC2032", "DRN2001", "PEP1001", "RPR3600"]
    assert all(r.method == "wordbox" for r in rows.values())


def test_documented_row_rpr3542(rows):
    r = rows["RPR3542"]
    assert (r.cost_code, r.team, r.name) == ("W5033", "621", "Warwick Road (westbound)")
    assert r.description == "Resurfacing and rehabilitation works to road centreline"
    assert (r.suburb, r.ward) == ("Warwick", "South-East")
    assert r.expenditure == 406364
    assert r.funding_split == {"municipal": 135455, "reserve": 0, "trust": 0, "grants_new": 270909, "grants_cfwd": 0,
                               "contribution": 0, "brought_fwd": 0, "loan": 0}
    assert r.flags == ["wrapped_text_join"]                  # a description line came from above the anchor


def test_wrapped_name_and_description(rows):
    r = rows["RDC2032"]
    assert r.name == "Eddystone Ave - Joondalup to Honeybush"
    assert r.description == "Road upgrade and dual carriageway"
    assert (r.suburb, r.ward, r.expenditure) == ("Joondalup", "North", 750000)
    d = rows["DRN2001"]
    assert d.description == "Upgrade of stormwater drainage pits and pipes including new soak wells and gully"
    assert "carry_forward" in d.flags and d.funding_split["brought_fwd"] == 100000


def test_headings_totals_and_footer_ignored(rows):
    assert all("Total" not in r.name and "Program" not in r.name and "Construction" not in r.name for r in rows.values())
    assert rows["RPR3550"].name == "Marmion Avenue (northbound)" and rows["RPR3550"].flags == []
    assert all("City of Joondalup" not in r.description for r in rows.values())


def test_continuation_page_and_sum_check(rows):
    assert rows["PEP1001"].description == "Playground equipment replacement"
    assert rows["PEP1001"].page == 2
    assert "value_ambiguous_column" in rows["RPR3600"].flags           # 100,000 split vs 120,000 total


def test_mentions(pdf_path, source):
    ms = {m.council_project_code: m for m in jb.extract(extract_pdf(pdf_path), source, now=NOW)}
    m = ms["RPR3542"]
    assert m.value_amount == 406364 and m.value_basis == "budget_total" and m.value_gst == "unknown"
    assert (m.cost_code, m.suburb, m.ward) == ("W5033", "Warwick", "South-East")
    assert (m.road_name, m.direction) == ("warwick road", "westbound")
    assert m.funding_refs == ["Municipal", "Grant"]
    assert m.stage == "adopted_budget" and m.financial_year == "2026-27" and m.page == 1
    assert m.bbox and len(m.bbox) == 4 and m.quote.startswith("RPR3542 – Warwick Road (westbound)")
    assert {"code": "ROAD_RESURF", "method": "rule", "evidence": "prefix:RPR"} in m.trade_labels
    assert m.uncertainty_flags == ["wrapped_text_join"] and m.confidence == pytest.approx(0.7)
    e = ms["RDC2032"]
    assert (e.road_name, e.from_road, e.to_road) == ("eddystone avenue", "joondalup", "honeybush")
    assert "ROAD_CONST" in {t["code"] for t in e.trade_labels}
    assert "DRAINAGE" in {t["code"] for t in ms["DRN2001"].trade_labels}
    assert "CarryForward" in ms["DRN2001"].funding_refs
    assert {t["code"] for t in ms["PEP1001"].trade_labels} == {"NON_CIVIL"}
    assert len({x.mention_id for x in ms.values()}) == 6
    again = jb.extract(extract_pdf(pdf_path), source, now=NOW)
    assert sorted(x.mention_id for x in again) == sorted(x.mention_id for x in ms.values())


# ---- text-layer fallback ----------------------------------------------------------------------
@pytest.fixture
def layout_text():
    return "".join(ln for ln in FIX.read_text().splitlines(keepends=True) if not ln.startswith("#"))


def test_layout_fixture_rows(layout_text):
    rows = {r.code: r for r in jb.parse_layout_text(5, layout_text)}
    assert list(rows) == ["RPR3542", "RDC2032"]
    r = rows["RPR3542"]
    assert (r.cost_code, r.team, r.name, r.suburb, r.ward) == ("W5033", "621", "Warwick Road (westbound)", "Warwick", "South-East")
    assert r.description == "Resurfacing and rehabilitation works"
    assert r.expenditure == 406364 and r.values["municipal"] == 135455 and r.values["grants_new"] == 270909
    assert set(r.flags) == {"value_ambiguous_column", "wrapped_text_join"}
    e = rows["RDC2032"]
    assert (e.name, e.suburb, e.ward, e.expenditure) == ("Eddystone Ave - Joondalup to Honeybush", "Joondalup", "North", 750000)
    assert e.values["municipal"] == 509102
    assert layout_text[r.char_start:r.char_end].count("RPR3542") == 1


def test_fallback_used_when_page_has_no_words(layout_text, source):
    dt = DocText("pdf", pages=[PageText(1, layout_text, [], source="pdftotext")])
    ms = {m.council_project_code: m for m in jb.extract(dt, source, now=NOW)}
    assert set(ms) == {"RPR3542", "RDC2032"}
    m = ms["RPR3542"]
    assert m.value_amount == 406364 and m.suburb == "Warwick"
    assert "value_ambiguous_column" in m.uncertainty_flags and m.bbox is None
    assert layout_text[m.char_start:m.char_end].startswith("                                                  Resurfacing")


def test_fallback_from_words_when_header_missing(tmp_path, source):
    """No header anywhere in the document: layout lines are rebuilt from the word boxes."""
    doc = pymupdf.open()
    page = doc.new_page(width=1190, height=842)
    _row(page, 130, "RPR3542", "W5033", "621", ["Warwick Road (westbound)"], ["Resurfacing"], "Warwick", "South-East",
         ["135,455", "-", "-", "270,909", "-", "-", "-", "-", "406,364"])
    doc.save(tmp_path / "nohdr.pdf")
    dt = extract_pdf(tmp_path / "nohdr.pdf")
    (m,) = jb.extract(dt, source, now=NOW)
    assert (m.council_project_code, m.cost_code, m.value_amount, m.suburb, m.ward) == \
        ("RPR3542", "W5033", 406364, "Warwick", "South-East")
    assert m.funding_split["grants_new"] == 270909 and "value_ambiguous_column" in m.uncertainty_flags


def test_classify_label():
    assert jb.classify_label("Grants New") == "grants_new"
    assert jb.classify_label("Grants C/fwd") == "grants_cfwd"
    assert jb.classify_label("Project Number") == "project_number"
    assert jb.classify_label("Project Name") == "project_name"
    assert jb.classify_label("Brought Fwd") == "brought_fwd"
    assert jb.classify_label("Cost Code") == "cost_code"


def test_registry_and_ingest(pdf_path, tmp_path):
    assert get_extractor("joondalup_budget") is jb.extract
    conn = db.connect(":memory:")
    doc, n = extract_document(conn, pdf_path, council_id="joondalup", url=URL, extractor="joondalup_budget",
                              doc_type="budget", financial_year="2026-27", cache_dir=tmp_path / "cache")
    assert n == 6 and doc.extract_status == "ok"
    stored = {m.council_project_code: m for m in db.mentions_for_document(conn, doc.doc_id)}
    assert stored["RPR3542"].value_amount == 406364
    assert stored["RPR3542"].funding_split["municipal"] == 135455
