"""M17: Black Spot cross-check: tag council projects, never create projects, idempotent (offline)."""

from datetime import datetime, timezone

import pytest

from radar import blackspot, changes, db, dedupe, digest, export
from radar.extract import blackspot as bs_extract
from radar.models import ProjectMention, SourceDocument, make_mention_id
from radar.text import DocText, PageText

ROAD = [{"code": "ROAD_CONST", "method": "rule", "evidence": "kw:roundabout"}]
NOW = datetime(2026, 11, 22, 3, 0, tzinfo=timezone.utc)
BS_TEXT = """Preston Point Road-Stock Road (City of Melville)
Install roundabout
$450,000
Hartman Drive SLK 3.52 - 3.84 (City of Wanneroo)
Shoulder sealing
$212,500
Lawley Street-Lockwood Street (City of Stirling)
Signals
$1,200,000
Nowhere Road-Elsewhere Street (City of Melville)
Roundabout
$100,000
"""


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    d = SourceDocument(doc_id="m" * 64, doc_key="https://melville.example/cwp", council_id="melville",
                       source_url="https://melville.example/cwp", doc_type="capital_works_program",
                       retrieved_at="2026-11-20T00:00:00+00:00")
    db.upsert_source_document(c, d)
    w = SourceDocument(doc_id="w" * 64, doc_key="https://wanneroo.example/b", council_id="wanneroo",
                       source_url="https://wanneroo.example/b", doc_type="budget", retrieved_at="2026-11-20T00:00:00+00:00")
    db.upsert_source_document(c, w)
    for doc, anchor, name, kw in (
            (d, "1", "Preston Point Road and Stock Road intersection upgrade", {}),
            (w, "2", "Hartman Drive resurfacing", {"road_name": "Hartman Drive"}),
            (d, "3", "Unrelated Road works", {"road_name": "Unrelated Road"})):
        db.upsert_mention(c, ProjectMention(
            mention_id=make_mention_id(doc.doc_id, "test", anchor), doc_id=doc.doc_id, council_id=doc.council_id, page=1,
            char_start=0, char_end=1, project_name_raw=name, extractor="test", trade_labels=ROAD, value_amount=900000,
            value_basis="current_year", stage="adopted_budget", funding_refs=["Municipal"], **kw))
    mr = SourceDocument(doc_id="r" * 64, doc_key="https://assets.mainroads.example/bs.pdf", council_id="mainroads",
                        source_url="https://assets.mainroads.example/bs.pdf", doc_type="blackspot_list",
                        financial_year="2026-27", retrieved_at="2026-11-21T00:00:00+00:00")
    db.upsert_source_document(c, mr)
    db.upsert_mentions(c, bs_extract.extract(DocText("pdf", pages=[PageText(1, BS_TEXT)]), mr))
    return c


def test_tags_matching_projects_only(conn):
    dedupe.link(conn)
    before = db.count(conn, __import__("radar.models", fromlist=["Project"]).Project)
    assert before == 3                                  # blackspot rows created no projects
    assert blackspot.tag(conn) == 2                     # Preston/Stock (intersection) + Hartman (road); others unmatched
    links = {l.mention_id: l for l in db.all_links(conn)}
    bs = {m.project_name_raw: m for m in db.all_mentions(conn) if m.extractor == "blackspot"}
    assert links[bs["Preston Point Road-Stock Road"].mention_id].link_method == "blackspot_xref"
    assert bs["Lawley Street-Lockwood Street"].mention_id not in links
    assert bs["Nowhere Road-Elsewhere Street"].mention_id not in links
    assert db.count(conn, __import__("radar.models", fromlist=["Project"]).Project) == 3

    proj = db.get_project(conn, links[bs["Preston Point Road-Stock Road"].mention_id].project_id)
    assert proj.canonical_name.startswith("Preston Point Road and Stock Road")       # name/value not taken from Main Roads
    assert proj.latest_value == 900000

    data = export.build_data(conn, now=NOW, blackspot_enabled=True)
    p = next(x for x in data["projects"] if x["id"] == proj.project_id)
    assert "BlackSpot_State" in p["funding"] and "Municipal" in p["funding"]
    assert any(s["extractor"] == "blackspot" and s["url"].startswith("https://assets.mainroads") for s in p["sources"])
    assert data["blackspot_note"] and "licence" in data["blackspot_note"].lower()
    assert export.build_data(conn, now=NOW)["blackspot_note"] is None
    other = next(x for x in data["projects"] if x["name"] == "Unrelated Road works")
    assert "BlackSpot_State" not in other["funding"]

    item = digest.build_item(digest.load_data(conn), proj)
    assert "BlackSpot_State" in item["funding"] and item["source"]["label"].startswith("City of Melville, ")


def test_idempotent(conn):
    dedupe.link(conn)
    blackspot.tag(conn)
    snap = ([(l.mention_id, l.project_id, l.link_method) for l in db.all_links(conn)],
            [(p.project_id, p.latest_value, p.canonical_name, p.latest_stage) for p in db.all_projects(conn)])
    rep = dedupe.link(conn)
    assert sum(rep.linked.values()) == 0 and blackspot.tag(conn) == 0
    assert snap == ([(l.mention_id, l.project_id, l.link_method) for l in db.all_links(conn)],
                    [(p.project_id, p.latest_value, p.canonical_name, p.latest_stage) for p in db.all_projects(conn)])


def test_rebuild_relinks(conn):
    dedupe.link(conn)
    blackspot.tag(conn)
    n = len(db.all_links(conn))
    dedupe.link(conn, rebuild=True)
    blackspot.tag(conn)
    assert len(db.all_links(conn)) == n


def test_slk_overlap_gate(conn):
    # a council mention that states a non-overlapping SLK range must not be tagged
    w = db.get_source_document(conn, "w" * 64)
    db.upsert_mention(conn, ProjectMention(
        mention_id=make_mention_id(w.doc_id, "test", "2"), doc_id=w.doc_id, council_id="wanneroo", page=1, char_start=0,
        char_end=1, project_name_raw="Hartman Drive resurfacing", extractor="test", trade_labels=ROAD,
        road_name="Hartman Drive", slk_from=9.0, slk_to=9.5))
    dedupe.link(conn)
    assert blackspot.tag(conn) == 1


def test_road_only_match_needs_a_single_candidate(conn):
    w = db.get_source_document(conn, "w" * 64)
    db.upsert_mention(conn, ProjectMention(
        mention_id=make_mention_id(w.doc_id, "test", "9"), doc_id=w.doc_id, council_id="wanneroo", page=1, char_start=0,
        char_end=1, project_name_raw="Hartman Drive footpath renewal (north side)", extractor="test", road_name="Hartman Drive",
        trade_labels=[{"code": "FOOTPATH", "method": "rule", "evidence": "kw:footpath"}], value_amount=50000))
    dedupe.link(conn)
    assert len([p for p in db.all_projects(conn, "wanneroo") if p.road_name == "Hartman Drive"]) == 2
    assert blackspot.tag(conn) == 1                     # Preston/Stock only; Hartman is ambiguous -> not tagged
