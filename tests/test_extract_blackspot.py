"""M17: Black Spot extractor on a hand-built page-text fixture (offline)."""

from pathlib import Path

import pytest

from radar.extract import blackspot, get_extractor
from radar.models import SourceDocument
from radar.text import DocText, PageText

FIX = Path(__file__).parent / "fixtures" / "blackspot_pages.txt"
URL = "https://assets.mainroads.wa.gov.au/black-spot-2026-27.pdf"


@pytest.fixture
def doc():
    return DocText("pdf", pages=[PageText(i, t) for i, t in enumerate(FIX.read_text().split("=====PAGE=====\n"), start=1)])


@pytest.fixture
def source():
    return SourceDocument(doc_id="b" * 64, doc_key=URL, council_id="mainroads", source_url=URL,
                          doc_type="blackspot_list", financial_year="2026-27")


@pytest.fixture
def mentions(doc, source):
    return blackspot.extract(doc, source, now="2026-11-22T00:00:00+00:00")


def by_name(mentions):
    return {m.project_name_raw: m for m in mentions}


def test_registered():
    assert get_extractor("blackspot") is blackspot.extract


def test_only_target_councils_kept(mentions):
    assert sorted(by_name(mentions)) == ["Ardross Street-Munro Road", "Hartman Drive",
                                         "Lawley Street-Lockwood Street", "Preston Point Road-Stock Road",
                                         "Unknown Road-Mystery Street"]
    assert {m.council_id for m in mentions} == {"melville", "wanneroo", "stirling"}      # Esperance dropped


def test_intersection_row(mentions):
    m = by_name(mentions)["Preston Point Road-Stock Road"]
    assert m.council_id == "melville" and m.page == 1
    assert (m.road_name, m.location_raw) == ("Preston Point Road", "Preston Point Road / Stock Road")
    assert m.value_amount == 450000 and m.value_basis == "current_year"
    assert m.funding_refs == ["BlackSpot_State"] and m.stage == "adopted_budget" and m.financial_year == "2026-27"
    assert m.extractor == "blackspot" and m.slk_from is None
    assert "roundabout" in m.quote and m.char_end > m.char_start


def test_slk_row_amount_on_description_line(mentions):
    m = by_name(mentions)["Hartman Drive"]
    assert m.council_id == "wanneroo" and m.road_name == "Hartman Drive"
    assert (m.slk_from, m.slk_to) == (3.52, 3.84)
    assert m.value_amount == 212500
    assert "audible edge lines" in m.quote and m.quote.endswith("$212,500")


def test_amount_with_trailing_text_and_multiline_description(mentions):
    m = by_name(mentions)["Lawley Street-Lockwood Street"]
    assert m.council_id == "stirling" and m.value_amount == 1200000
    assert m.location_raw == "Lawley Street / Lockwood Street"


def test_wrapped_heading_and_missing_amount(mentions):
    m = by_name(mentions)["Unknown Road-Mystery Street"]
    assert m.value_amount is None and "value_missing" in m.uncertainty_flags


def test_wrapped_council_bracket_for_target():
    doc = DocText("pdf", pages=[PageText(1, "Hepburn Avenue-Marmion Avenue\n(City of Joondalup)\nNew roundabout\n$600,000\n")])
    src = SourceDocument(doc_id="d" * 64, doc_key=URL, council_id="mainroads", source_url=URL, doc_type="blackspot_list")
    (m,) = blackspot.extract(doc, src)
    assert m.council_id == "joondalup" and m.value_amount == 600000 and m.road_name == "Hepburn Avenue"


def test_mention_ids_stable(doc, source):
    a = [m.mention_id for m in blackspot.extract(doc, source)]
    assert a == [m.mention_id for m in blackspot.extract(doc, source)] and len(set(a)) == len(a)


def test_parenthesised_slk_as_in_the_2026_27_list():
    doc = DocText("pdf", pages=[PageText(1, "Kemp Street (SLK 0.00 - 1.26) (City of Wanneroo)\nShoulder sealing\n$195,994\n")])
    src = SourceDocument(doc_id="d" * 64, doc_key=URL, council_id="mainroads", source_url=URL, doc_type="blackspot_list")
    (m,) = blackspot.extract(doc, src)
    assert (m.road_name, m.slk_from, m.slk_to, m.value_amount) == ("Kemp Street", 0.0, 1.26, 195994)
