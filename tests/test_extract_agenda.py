"""M11: agenda / minutes item extractor on per-council text fixtures (offline).

Fixtures in ``fixtures/agenda/`` are synthetic, in each council's documented style; the header comment of
each file lists which facts come from RESEARCH.md.
"""

from pathlib import Path

import pytest

from radar.extract import agenda_items as ai, get_extractor
from radar.models import SourceDocument
from radar.text import DocText, PageText, extract_html

FIX = Path(__file__).parent / "fixtures" / "agenda"
NOW = "2026-11-22T00:00:00+00:00"


def load_pages(name: str) -> DocText:
    text = "".join(ln for ln in (FIX / name).read_text().splitlines(keepends=True) if not ln.startswith("#"))
    return DocText("pdf", pages=[PageText(i, t) for i, t in enumerate(text.split("=====PAGE=====\n"), start=1)])


def src(council: str, doc_type: str) -> SourceDocument:
    return SourceDocument(doc_id=council[0] * 64, doc_key=f"https://{council}.example/doc", council_id=council,
                          source_url=f"https://{council}.example/doc", doc_type=doc_type, financial_year="2026-27")


@pytest.fixture
def stirling():
    return load_pages("stirling_minutes_pages.txt")


@pytest.fixture
def rockingham():
    return load_pages("rockingham_minutes_pages.txt")


@pytest.fixture
def wanneroo():
    return extract_html((FIX / "wanneroo_oc_agenda.htm").read_bytes())


# ---- segmentation -------------------------------------------------------------------------------
def test_stirling_items_split_and_toc_skipped(stirling):
    flat, items = ai.segment_items(stirling, ai.item_pattern_for("stirling"))
    assert [(i.item_id, i.title) for i in items] == [
        ("12.1/CR3", "Tender – 9 Leeway Court, Osborne Park Roof and HVAC"),
        ("12.2/IS1", "Erindale Road Resurfacing – Budget Amendment"),
        ("12.3/CR4", "Community Garden Licence – Inglewood")]
    assert flat.locate(items[0].start)[0] == 2          # the body heading, not the contents line on page 1
    assert "CARRIED 13/0" in items[0].body(flat)          # item 12.1 runs across the page break


def test_wanneroo_items_split_toc_rows_skipped(wanneroo):
    flat, items = ai.segment_items(wanneroo, ai.item_pattern_for("wanneroo"))
    assert [i.item_id for i in items] == ["AS01-11/26", "AS02-11/26", "CS01-11/26"]
    assert items[0].title == "Tender No. 26173 The Supply and Laying of Asphalt for Road Resurfacing"
    assert all(not flat.skipped(i.start) for i in items)
    assert "Recommendation" in items[0].body(flat)       # the heading block, not the TOC table row


def test_generic_items_split(rockingham):
    _, items = ai.segment_items(rockingham, ai.item_pattern_for("rockingham"))
    assert [i.item_id for i in items] == ["13.1", "13.2", "13.3"]


def test_item_patterns_from_config():
    assert ai.item_pattern_for("stirling") == r"\d{1,2}\.\d{1,2}/[A-Z]{1,5}\d{1,3}"
    assert ai.item_pattern_for("wanneroo") == r"[A-Z]{2}\d{2}-\d{2}/\d{2}"
    assert ai.item_pattern_for("rockingham") == ai.GENERIC_PATTERN


def test_civil_filter(stirling, rockingham):
    flat, items = ai.segment_items(stirling, ai.item_pattern_for("stirling"))
    assert [ai.is_civil(i, flat) for i in items] == [True, True, False]
    flat, items = ai.segment_items(rockingham, ai.GENERIC_PATTERN)
    assert [ai.is_civil(i, flat) for i in items] == [True, True, False]


# ---- Stirling minutes (tender report template) -------------------------------------------------
def test_stirling_tender_parsed_and_recommendation_collapsed(stirling):
    ms = ai.extract(stirling, src("stirling", "minutes"), now=NOW)
    assert [m.resolution_ref for m in ms] == ["12.1/CR3", "12.2/IS1"]          # one mention per item
    k = ms[0]
    assert k.supplier_name == "Kardan Australia Pty Ltd"
    assert (k.value_amount, k.value_gst, k.value_basis) == (1064854, "ex", "contract_award")
    assert k.stage == "tender_awarded" and k.uncertainty_flags == []
    assert k.page == 3 and k.quote.startswith("That the tender from Kardan")      # the council resolution wins
    assert k.date_stated == "Monday 21 September 2026"
    assert k.suburb == "Osborne Park"
    assert {t["code"] for t in k.trade_labels} == {"NON_CIVIL"}                 # roof + HVAC: still extracted


def test_stirling_budget_amendment(stirling):
    b = ai.extract(stirling, src("stirling", "minutes"), now=NOW)[1]
    assert (b.value_amount, b.value_basis, b.stage) == (180000, "estimate", "budget_review")
    assert (b.road_name, b.from_road, b.to_road, b.suburb) == ("erindale road", "beach road", "karrinyup road", "Hamersley")
    assert "gst_unknown" in b.uncertainty_flags and b.funding_refs == ["Reserve"]
    assert "ROAD_RESURF" in {t["code"] for t in b.trade_labels}


# ---- Wanneroo Infocouncil HTML agenda ----------------------------------------------------------
def test_wanneroo_tender_no_supplier_value(wanneroo):
    ms = ai.extract(wanneroo, src("wanneroo", "agenda"), now=NOW)
    assert [m.resolution_ref for m in ms] == ["AS01-11/26", "AS02-11/26"]
    t = ms[0]
    assert (t.tender_no, t.supplier_name) == ("26173", "Example Asphalt Pty Ltd")
    assert (t.value_amount, t.value_gst, t.value_basis) == (2400000, "ex", "contract_award")
    assert t.stage == "tender_advertised" and "agenda_not_resolution" in t.uncertainty_flags   # agenda, not minutes
    page_text = wanneroo.pages[0].text
    assert page_text[t.char_start:t.char_end].startswith("1. ACCEPTS Tender No. 26173")
    assert "ROAD_RESURF" in {x["code"] for x in t.trade_labels}


def test_wanneroo_intersection_budget_variation(wanneroo):
    bs = ai.extract(wanneroo, src("wanneroo", "agenda"), now=NOW)[1]
    assert bs.location_raw == "joondalup drive / wanneroo road"
    assert (bs.value_amount, bs.stage) == (350000, "budget_review")
    assert "BlackSpot_State" in bs.funding_refs
    assert "INTERSECTION" in {x["code"] for x in bs.trade_labels}


# ---- generic numbering (Rockingham-style; real Rockingham numbering not documented) ------------
def test_amended_value_resolution_wins(rockingham):
    ms = ai.extract(rockingham, src("rockingham", "minutes"), now=NOW)
    t = ms[0]
    assert (t.tender_no, t.supplier_name, t.value_amount) == ("T26/27-05", "Example Civil Pty Ltd", 850000)
    assert len([m for m in ms if m.resolution_ref == "13.1"]) == 1      # officer 900k, committee + council 850k
    d = ms[1]
    assert d.stage == "deferred" and d.value_amount is None and "value_missing" in d.uncertainty_flags
    assert d.road_name == "safety bay road" and d.suburb is None         # "Safety Bay" inside a road name is not a suburb


def test_dedupe_facts_rules():
    rec = ai.Fact("recommendation", 0, 10, tender_no="1", value=100.0, award=True, supplier="A")
    res = ai.Fact("resolution", 20, 30, tender_no="1", value=100.0, award=True)
    (only,) = ai.dedupe_facts([rec, res])
    assert only.section == "resolution" and only.supplier == "A"
    amended = ai.Fact("resolution", 20, 30, tender_no="1", value=90.0, award=True)
    assert [f.value for f in ai.dedupe_facts([rec, amended])] == [90.0]
    other = ai.Fact("recommendation", 40, 50, tender_no="2", value=5.0, award=True)
    assert [f.tender_no for f in ai.dedupe_facts([rec, other])] == ["1", "2"]


def test_tender_number_formats():
    assert ai.tender_no("Tender No. 26173 The Supply") == "26173"
    assert ai.tender_no("ACCEPTS Tender T26/27-05 from") == "T26/27-05"
    assert ai.tender_no("Tender 12/2026 – Roof") == "12/2026"
    assert ai.tender_no("Tender Deadline: Monday 21 September 2026") is None
    assert ai.tender_no("the tender from Kardan") is None


def test_doc_kind_and_provenance(stirling):
    s = src("stirling", "other")
    assert ai.doc_kind(stirling, s) == "minutes"
    ms = ai.extract(stirling, s, now=NOW)
    for m in ms:
        text = stirling.pages[m.page - 1].text
        assert 0 <= m.char_start < m.char_end <= len(text) and len(m.quote) <= 300
        assert m.extractor == "agenda_items" and m.extracted_at == NOW and m.doc_id == "s" * 64
    again = ai.extract(stirling, s, now=NOW)
    assert [m.mention_id for m in again] == [m.mention_id for m in ms]
    assert len({m.mention_id for m in ms}) == len(ms)


def test_registry():
    assert get_extractor("agenda_items") is ai.extract
