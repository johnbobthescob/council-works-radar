"""M4: table-driven normaliser tests; examples come from RESEARCH.md §5 / PLAN.md §5.2."""

import pytest

from radar import normalise as N


@pytest.mark.parametrize("text,expected", [
    ("$406,364", 406364.0),
    ("406,364", 406364.0),
    ("135,455", 135455.0),
    ("$4.3 million", 4_300_000.0),
    ("$4.3M", 4_300_000.0),
    ("$1.2m", 1_200_000.0),
    ("$250k", 250_000.0),
    ("$20.4 million", 20_400_000.0),
    ("(1,234)", -1234.0),
    ("($1,234.50)", -1234.5),
    ("-", 0.0),
    ("–", 0.0),
    ("$ -", 0.0),
    ("$0", 0.0),
    ("", None),
    (None, None),
    ("n/a", None),
    ("(1,234", None),
    ("12 apples", None),
])
def test_parse_money(text, expected):
    assert N.parse_money(text) == expected


def test_find_money_in_running_text():
    t = "That the tender from Acme be accepted for $1,234,567.00 (ex GST), budget $4.3 million."
    hits = N.find_money(t)
    assert [h.amount for h in hits] == [1234567.0, 4_300_000.0]
    assert t[hits[0].start:hits[0].end].startswith("$1,234,567")
    assert N.find_money("no money here, 2026 and 1,000 widgets") == []


@pytest.mark.parametrize("text,expected", [
    ("$1,234,567 (ex GST)", "ex"),
    ("$1,234,567 excluding GST", "ex"),
    ("$100,000 excl. GST", "ex"),
    ("$100,000 (inc GST)", "inc"),
    ("$100,000 including GST", "inc"),
    ("$100,000 GST inclusive", "inc"),
    ("$100,000", "unknown"),
    ("ex GST or inc GST depending", "unknown"),
    (None, "unknown"),
])
def test_gst_basis(text, expected):
    assert N.gst_basis(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("2026-27", "2026-27"),
    ("2026/27", "2026-27"),
    ("26/27", "2026-27"),
    ("FY2026-27", "2026-27"),
    ("FY 26/27 Capital Works", "2026-27"),
    ("2026-2027", "2026-27"),
    ("Budget-2026-27.pdf", "2026-27"),
    ("2026-30", None),            # multi-year range is not a single FY
    ("12/10/2026", None),         # dates are not FYs
    ("$1,2026-27", None),
    ("no year", None),
])
def test_parse_fy(text, expected):
    assert N.parse_fy(text) == expected


def test_strip_fy():
    assert " ".join(N.strip_fy("Alvan Street Resurfacing 26/27").split()) == "Alvan Street Resurfacing"
    assert " ".join(N.strip_fy("Road Program 2026-27 Stage 2").split()) == "Road Program Stage 2"


@pytest.mark.parametrize("text,expected", [
    ("Warwick Rd", "warwick road"),
    ("Eddystone Ave", "eddystone avenue"),
    ("Leach Hwy.", "leach highway"),
    ("Hepburn Ave,  Marmion Ave", "hepburn avenue marmion avenue"),
    ("Elderberry Dr", "elderberry drive"),
    ("O'Connor Cres", "oconnor crescent"),
    ("St Andrews Dr", "st andrews drive"),            # leading St is Saint
    ("Smith St", "smith street"),
    ("Mitchell Fwy  – Ocean Reef Rd", "mitchell fwy ocean reef road"),
    ("Warwick Road (westbound)", "warwick road"),
    ("Alvan St Resurfacing 26/27", "alvan street resurfacing"),
    ("Marmion-Ave", "marmion avenue"),
    ("", ""),
    (None, ""),
])
def test_normalise_road(text, expected):
    assert N.normalise_road(text) == expected


@pytest.mark.parametrize("text,name,direction", [
    ("Warwick Road (westbound)", "Warwick Road", "westbound"),
    ("Warwick Road (WB)", "Warwick Road", "westbound"),
    ("Mitchell Fwy northbound", "Mitchell Fwy", "northbound"),
    ("Warwick Road", "Warwick Road", None),
    ("Smith Rd sb extension", "Smith Rd sb extension", None),   # bare 2-letter code not trusted
])
def test_extract_direction(text, name, direction):
    assert N.extract_direction(text) == (name, direction)


def test_normalise_road_with_direction():
    assert N.normalise_road_with_direction("Warwick Rd (westbound)") == ("warwick road", "westbound")


@pytest.mark.parametrize("text,road,frm,to", [
    ("Elderberry Dr – South Lake to Berrigan Dr", "elderberry drive", "south lake", "berrigan drive"),
    ("Elderberry Dr - South Lake to Berrigan Dr", "elderberry drive", "south lake", "berrigan drive"),
    ("Eddystone Ave - Joondalup to Honeybush", "eddystone avenue", "joondalup", "honeybush"),
    ("Resurfacing of Elderberry Dr – South Lake to Berrigan Dr", "elderberry drive", "south lake", "berrigan drive"),
    ("Warwick Road between Erindale Road and Beach Road", "warwick road", "erindale road", "beach road"),
    ("Hepburn Avenue – Marmion Avenue to Whitfords Avenue.", "hepburn avenue", "marmion avenue", "whitfords avenue"),
])
def test_parse_segment(text, road, frm, to):
    seg = N.parse_segment(text)
    assert seg is not None and seg.key == (road, frm, to)


def test_parse_segment_direction_and_none():
    assert N.parse_segment("Warwick Rd (westbound) - Erindale Rd to Beach Rd").direction == "westbound"
    assert N.parse_segment("General footpath program") is None
    assert N.parse_segment("") is None


@pytest.mark.parametrize("text", [
    "Hepburn Ave / Marmion Ave",
    "Marmion Avenue and Hepburn Avenue",
    "Intersection of Hepburn Ave & Marmion Ave",
    "Marmion Ave / Hepburn Ave roundabout",
])
def test_intersection_is_order_independent(text):
    assert N.parse_intersection(text) == ("hepburn avenue", "marmion avenue")


@pytest.mark.parametrize("text", ["Cost and benefit", "Hepburn Ave", "A Rd / B Rd / C Rd", "Smith Rd / Smith Road", None])
def test_intersection_rejects(text):
    assert N.parse_intersection(text) is None


def test_suburb_lookup(tmp_path):
    assert N.lookup_suburb("Warwick Road (westbound) ... Warwick", "joondalup") == "Warwick"
    assert N.lookup_suburb("works at Ocean Reef Road, Ocean Reef", "joondalup") == "Ocean Reef"
    assert N.lookup_suburb("South Lake drainage", "cockburn") == "South Lake"
    assert N.lookup_suburb("Hamilton Hill footpath") == "Hamilton Hill"
    assert N.lookup_suburb("Hamilton Hill footpath", "joondalup") is None
    assert N.lookup_suburb("Warwickshire Rd", "joondalup") is None      # whole words only
    assert N.lookup_suburb(None) is None
    assert N.lookup_suburb("anything", "no-such-council") is None


def test_suburb_files_exist_for_configured_councils():
    from radar.settings import load_councils
    for key in load_councils().councils:
        assert N.load_suburbs(key), f"missing config/suburbs/{key}.txt"
