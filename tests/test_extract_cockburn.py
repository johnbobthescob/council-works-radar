"""M10: Cockburn Project Plans extractor on a hand-built page-text fixture (offline)."""

from pathlib import Path

import pytest

from radar.extract import cockburn_plans as cp, get_extractor
from radar.models import SourceDocument
from radar.text import DocText, PageText

FIX = Path(__file__).parent / "fixtures" / "cockburn_plans_pages.txt"
URL = "https://www.cockburn.wa.gov.au/getattachment/91e79b42/ECM_12774885_v2_Project-Plans-2026-2027-pdf.aspx"


@pytest.fixture
def pages():
    return FIX.read_text().split("=====PAGE=====\n")


@pytest.fixture
def doc(pages):
    return DocText("pdf", pages=[PageText(i, t) for i, t in enumerate(pages, start=1)])


@pytest.fixture
def source():
    return SourceDocument(doc_id="c" * 64, doc_key=URL, council_id="cockburn", source_url=URL, doc_type="project_plans")


@pytest.fixture
def mentions(doc, source):
    return cp.extract(doc, source, now="2026-11-22T00:00:00+00:00")


def test_parse_program_page(pages):
    plan = cp.parse_plan_page(2, pages[1])
    assert (plan.project_id, plan.title, plan.directorate) == ("4C.18", "Roads Resurfacing Program", "Infrastructure Services")
    assert (plan.total_budget, plan.current_budget) == (2850000, 2850000)
    assert (plan.start_raw, plan.end_raw) == ("July 2026", "June 2027")
    assert "resurface and rehabilitate" in plan.description and "Locations" not in plan.description
    assert [s.line for s in plan.segments] == [
        "Elderberry Dr – South Lake to Berrigan Dr", "Warton Rd – Jandakot Rd to Nicholson Rd",
        "Beeliar Drive – Wentworth to Midgeegooroo Ave"]


def test_contents_page_skipped(pages):
    assert cp.parse_plan_page(1, pages[0]) is None


def test_project_ids_and_budgets(mentions):
    parents = {m.council_project_code: m for m in mentions if "#" not in m.council_project_code}
    assert set(parents) == {"4C.18", "3B.07", "5D.02"}
    prog = parents["4C.18"]
    assert prog.project_name_raw == "Roads Resurfacing Program" and prog.page == 2
    assert prog.value_amount == 2850000 and prog.value_basis == "current_year"
    assert prog.uncertainty_flags == []          # total == current year; segments give it a location
    assert prog.funding_refs == ["R2R", "Municipal"]
    skate = parents["3B.07"]
    assert skate.value_amount == 400000 and skate.value_basis == "current_year"     # current-year budget wins
    assert "multi_year" in skate.uncertainty_flags                                  # total 1.2M differs
    assert (skate.date_start, skate.date_end) == ("2026-09-01", "2028-06-30")
    nothing = parents["5D.02"]
    assert nothing.value_amount is None and "value_missing" in nothing.uncertainty_flags
    assert nothing.date_start == "2027-03-01" and nothing.date_end is None
    assert prog.date_start == "2026-07-01" and prog.date_end == "2027-06-30" and prog.date_stated == "July 2026 – June 2027"
    assert prog.financial_year == "2026-27" and prog.stage == "adopted_budget"


def test_segments_parsed_into_road_from_to(mentions):
    segs = [m for m in mentions if "#" in m.council_project_code]
    assert len(segs) == 3
    el = next(m for m in segs if m.project_name_raw.startswith("Elderberry"))
    assert (el.road_name, el.from_road, el.to_road) == ("elderberry drive", "south lake", "berrigan drive")
    assert el.council_project_code == "4C.18#elderberry-drive|south-lake|berrigan-drive"
    assert el.location_raw == "Elderberry Dr – South Lake to Berrigan Dr"
    assert el.value_amount is None and el.uncertainty_flags == ["value_missing"]
    assert el.date_start == "2026-07-01" and el.page == 2 and el.financial_year == "2026-27"
    bee = next(m for m in segs if m.project_name_raw.startswith("Beeliar"))
    assert (bee.road_name, bee.from_road, bee.to_road) == ("beeliar drive", "wentworth", "midgeegooroo avenue")
    assert all("ROAD_RESURF" in {t["code"] for t in m.trade_labels} for m in segs)


def test_provenance(mentions, pages):
    for m in mentions:
        text = pages[m.page - 1]
        assert m.char_start is not None and 0 <= m.char_start < m.char_end <= len(text)
        assert m.quote and len(m.quote) <= 300
        assert m.extractor == "cockburn_plans" and m.extractor_version == "1" and m.doc_id == "c" * 64
        assert m.extracted_at == "2026-11-22T00:00:00+00:00" and 0 <= m.confidence <= 1
    seg = next(m for m in mentions if m.project_name_raw.startswith("Warton"))
    assert pages[seg.page - 1][seg.char_start:seg.char_end] == "Warton Rd – Jandakot Rd to Nicholson Rd"


def test_unique_stable_ids(doc, source, mentions):
    assert len({m.mention_id for m in mentions}) == len(mentions) == 6
    assert [m.mention_id for m in cp.extract(doc, source)] == [m.mention_id for m in mentions]


def test_registry():
    assert get_extractor("cockburn_plans") is cp.extract


def test_label_variants():
    text = ("Kerb Replacement\nID: 2A.11\nTotal Budget\n$1.5 million\nCurrent Year Budget: $300,000\n"
            "Commencement Date: 14/08/2026\nCompletion Date: 30/06/2027\n")
    p = cp.parse_plan_page(5, text)
    assert (p.total_budget, p.current_budget) == (1500000, 300000)
    assert (p.start_raw, p.end_raw) == ("14/08/2026", "30/06/2027")
    two_ids = "A\nID: 1A.01\nB\nID: 2B.02\n"
    assert cp.parse_plan_page(1, two_ids) is None
