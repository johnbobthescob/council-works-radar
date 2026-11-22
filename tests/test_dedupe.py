"""M12: dedupe / linker key ladder, guards, fuzzy flagging, project state and idempotency (offline)."""

import pytest

from radar import cli, db, dedupe
from radar.models import Project, ProjectMention, SourceDocument, make_mention_id

RESURF = [{"code": "ROAD_RESURF", "method": "rule", "evidence": "kw:resurfac"}]
DRAIN = [{"code": "DRAINAGE", "method": "rule", "evidence": "kw:drainage"}]
INTER = [{"code": "INTERSECTION", "method": "rule", "evidence": "kw:roundabout"}]


@pytest.fixture
def conn():
    return db.connect(":memory:")


def doc(conn, name, council="joondalup", date="2026-07-01", doc_type="budget", key=None,
        retrieved="2026-11-22T00:00:00+00:00", last_modified=None):
    d = SourceDocument(doc_id=(name * 64)[:64], doc_key=key or f"https://example.org/{name}", council_id=council,
                       source_url=f"https://example.org/{name}", doc_type=doc_type, meeting_date=date,
                       retrieved_at=retrieved, http_last_modified=last_modified)
    db.upsert_source_document(conn, d)
    return d


def mention(conn, d, anchor, name, **kw):
    kw.setdefault("trade_labels", RESURF)
    m = ProjectMention(mention_id=make_mention_id(d.doc_id, "test", anchor), doc_id=d.doc_id, council_id=d.council_id,
                       page=1, char_start=0, char_end=1, project_name_raw=name, extractor="test",
                       extracted_at="2026-11-22T00:00:00+00:00", **kw)
    db.upsert_mention(conn, m)
    return m


def project_of(conn, m):
    link = db.get_link(conn, m.mention_id)
    return db.get_project(conn, link.project_id), link


def snapshot(conn):
    return ([tuple(r) for r in conn.execute("SELECT * FROM project ORDER BY project_id")],
            [tuple(r) for r in conn.execute("SELECT * FROM project_mention_link ORDER BY mention_id")],
            [tuple(r) for r in conn.execute("SELECT * FROM possible_duplicate ORDER BY project_a")])


# ---- 1. code -----------------------------------------------------------------------------------
def test_same_code_in_two_documents_merges(conn):
    budget = doc(conn, "a", date=None, retrieved="2026-07-10T00:00:00+00:00")
    review = doc(conn, "b", date="2027-02-24", doc_type="minutes")
    m1 = mention(conn, budget, "1", "Warwick Road (westbound)", council_project_code="RPR3542", value_amount=406364,
                 value_basis="budget_total", stage="adopted_budget", financial_year="2026-27", suburb="Warwick",
                 confidence=0.7)
    m2 = mention(conn, review, "2", "Warwick Road resurfacing", council_project_code="rpr 3542", value_amount=450000,
                 value_basis="budget_total", stage="budget_review", financial_year="2026-27", confidence=0.6)
    report = dedupe.link(conn)
    p1, l1 = project_of(conn, m1)
    p2, l2 = project_of(conn, m2)
    assert p1.project_id == p2.project_id and db.count(conn, Project) == 1
    assert (l1.link_method, l2.link_method) == ("singleton", "code")
    assert p1.dedupe_key == "code:RPR3542" and p1.dedupe_key_type == "code"
    assert (p1.latest_value, p1.latest_stage) == (450000, "budget_review")      # newer document wins
    assert p1.canonical_name == "Warwick Road (westbound)" and p1.suburb == "Warwick"   # highest confidence
    assert p1.trade_codes == ["ROAD_RESURF"] and not p1.review_needed
    assert report.linked == {"singleton": 1, "code": 1}


# ---- idempotency -------------------------------------------------------------------------------
def test_relink_is_idempotent_and_rebuild_reproduces(conn):
    d1, d2 = doc(conn, "a"), doc(conn, "b", date="2026-11-17", doc_type="minutes")
    mention(conn, d1, "1", "Warwick Road (westbound)", council_project_code="RPR3542", value_amount=1.0)
    mention(conn, d2, "2", "Warwick Road", council_project_code="RPR3542")
    mention(conn, d1, "3", "Hepburn Avenue Drainage Upgrade", trade_labels=DRAIN)
    mention(conn, d2, "4", "Hepburn Avenue drainage upgrade works", trade_labels=DRAIN)
    dedupe.link(conn)
    first = snapshot(conn)
    again = dedupe.link(conn)
    assert sum(again.linked.values()) == 0 and again.duplicates_new == 0
    assert snapshot(conn) == first
    dedupe.link(conn, rebuild=True)
    assert snapshot(conn) == first
    # a new mention for an existing code joins the project without disturbing the rest
    d3 = doc(conn, "c", date="2027-02-01", doc_type="minutes")
    m5 = mention(conn, d3, "5", "Warwick Rd", council_project_code="RPR3542")
    rep = dedupe.link(conn)
    assert rep.linked == {"code": 1} and db.count(conn, Project) == 3
    assert project_of(conn, m5)[0].dedupe_key == "code:RPR3542"


# ---- 2. segment + trade guard ------------------------------------------------------------------
def test_segment_match_with_trade_intersection_guard(conn):
    plans = doc(conn, "p", council="cockburn", date=None)
    agenda = doc(conn, "q", council="cockburn", date="2026-11-10", doc_type="agenda")
    seg = dict(road_name="elderberry drive", from_road="south lake", to_road="berrigan drive")
    parent = mention(conn, plans, "1", "Elderberry Dr – South Lake to Berrigan Dr",
                     council_project_code="4C.18#elderberry-drive|south-lake|berrigan-drive", **seg)
    resurf = mention(conn, agenda, "2", "Elderberry Drive resurfacing", road_name="Elderberry Dr",
                     from_road="South Lake", to_road="Berrigan Dr", tender_no="RFT 01/2026",
                     stage="tender_advertised")
    drain = mention(conn, agenda, "3", "Elderberry Drive drainage renewal", trade_labels=DRAIN, **seg)
    unknown = mention(conn, agenda, "4", "Elderberry Drive works",
                      trade_labels=[{"code": "UNKNOWN", "method": "rule", "evidence": "no_rule"}], **seg)
    dedupe.link(conn)
    pp, _ = project_of(conn, parent)
    pr, lr = project_of(conn, resurf)
    pd, ld = project_of(conn, drain)
    pu, lu = project_of(conn, unknown)
    assert pr.project_id == pp.project_id and lr.link_method == "segment"
    assert pr.latest_stage == "tender_advertised"
    assert pd.project_id != pp.project_id and ld.link_method in ("singleton", "fuzzy")    # not merged
    assert pd.dedupe_key == "segment:elderberry drive|south lake|berrigan drive#DRAINAGE"
    assert pu.project_id == pp.project_id                     # UNKNOWN trades are compatible with anything


def test_implied_labels_do_not_satisfy_the_guard():
    road_const = [{"code": "ROAD_CONST", "method": "rule", "evidence": "kw:upgrade"},
                  {"code": "DRAINAGE", "method": "rule_implied", "evidence": "implied:ROAD_CONST"}]
    assert dedupe.trade_set(road_const) == {"ROAD_CONST"}
    assert not dedupe.trades_compatible(dedupe.trade_set(road_const), {"DRAINAGE"})
    assert dedupe.trades_compatible({"UNKNOWN"}, {"DRAINAGE"})


# ---- 3. intersection (order-independent) -------------------------------------------------------
def test_intersection_key_is_order_independent(conn):
    # undated annual page fetched in the same run as the minutes: its Last-Modified dates it (July)
    cwp = doc(conn, "m", council="melville", date=None, doc_type="capital_program",
              last_modified="Wed, 01 Jul 2026 02:00:00 GMT")
    mins = doc(conn, "n", council="melville", date="2026-11-17", doc_type="minutes")
    a = mention(conn, cwp, "1", "Ardross Street and Munro Road Roundabout Installation", trade_labels=INTER,
                value_amount=700000, value_basis="budget_total", stage="adopted_budget", funding_refs=["BlackSpot_State"])
    b = mention(conn, mins, "2", "Tender 4/2026 Roundabout Construction", location_raw="munro road / ardross street",
                trade_labels=INTER, tender_no="4/2026", value_amount=655000, value_basis="contract_award",
                stage="tender_awarded")
    dedupe.link(conn)
    pa, _ = project_of(conn, a)
    pb, lb = project_of(conn, b)
    assert pa.project_id == pb.project_id
    assert {db.get_link(conn, a.mention_id).link_method, lb.link_method} == {"singleton", "intersection"}
    assert pa.dedupe_key == "intersection:ardross street|munro road"
    assert (pa.latest_value, pa.latest_value_basis, pa.latest_stage) == (655000, "contract_award", "tender_awarded")
    keys = dedupe.mention_keys(b)
    assert keys["intersection"] == dedupe.mention_keys(a)["intersection"]


# ---- 4. tender ---------------------------------------------------------------------------------
def test_tender_key_links_agenda_and_minutes(conn):
    agenda = doc(conn, "w", council="wanneroo", date="2026-11-24", doc_type="agenda")
    minutes = doc(conn, "x", council="wanneroo", date="2026-11-25", doc_type="minutes")
    a = mention(conn, agenda, "1", "Tender No. 26173 The Supply and Laying of Asphalt", tender_no="26173",
                value_amount=2400000, value_basis="contract_award", stage="tender_advertised",
                uncertainty_flags=["agenda_not_resolution"])
    b = mention(conn, minutes, "2", "Tender No 26173 – Supply and Laying of Asphalt", tender_no="26173",
                value_amount=2400000, value_basis="contract_award", stage="tender_awarded", supplier_name="X Pty Ltd")
    other_council = doc(conn, "y", council="stirling", date="2026-11-25", doc_type="minutes")
    c = mention(conn, other_council, "3", "Tender 26173", tender_no="26173")
    dedupe.link(conn)
    pa, _ = project_of(conn, a)
    pb, lb = project_of(conn, b)
    pc, _ = project_of(conn, c)
    assert pa.project_id == pb.project_id and lb.link_method == "tender"
    assert pa.latest_stage == "tender_awarded" and pa.dedupe_key == "tender:26173"
    assert pc.project_id != pa.project_id                      # keys are scoped per council
    assert "agenda_not_resolution" in pa.flags


# ---- 5. fuzzy: flagged, never merged -----------------------------------------------------------
def test_fuzzy_match_is_flagged_not_merged(conn):
    d1 = doc(conn, "a", date="2026-07-01")
    d2 = doc(conn, "b", date="2026-11-17", doc_type="minutes")
    m1 = mention(conn, d1, "1", "Hepburn Avenue Drainage Upgrade", trade_labels=DRAIN, financial_year="2026-27")
    m2 = mention(conn, d2, "2", "Hepburn Ave drainage upgrade works 2026/27", trade_labels=DRAIN, financial_year="2026-27")
    rep = dedupe.link(conn)
    p1, l1 = project_of(conn, m1)
    p2, l2 = project_of(conn, m2)
    assert p1.project_id != p2.project_id
    assert l2.link_method == "fuzzy" and l2.link_score >= 0.9
    for p in (p1, p2):
        assert p.review_needed and "possible_duplicate" in p.flags
    (pair,) = db.possible_duplicates(conn)
    assert {pair["project_a"], pair["project_b"]} == {p1.project_id, p2.project_id} and pair["method"] == "fuzzy"
    assert rep.duplicates_new == 1


def test_fuzzy_respects_fy_window_and_distinct_codes(conn):
    d1, d2 = doc(conn, "a"), doc(conn, "b", date="2026-11-17")
    mention(conn, d1, "1", "Warwick Road (westbound)", council_project_code="RPR3542", financial_year="2026-27")
    mention(conn, d1, "2", "Warwick Road (eastbound)", council_project_code="RPR3543", financial_year="2026-27")
    mention(conn, d1, "3", "Ocean Reef Road Footpath Renewal", financial_year="2026-27")
    mention(conn, d2, "4", "Ocean Reef Road Footpath Renewal", financial_year="2029-30")
    dedupe.link(conn)
    assert db.possible_duplicates(conn) == []                 # different codes; FYs 3 years apart
    assert db.count(conn, Project) == 4


def test_fuzzy_needs_two_shared_tokens(conn):
    d1, d2 = doc(conn, "a"), doc(conn, "b", date="2026-11-17")
    mention(conn, d1, "1", "Road")
    mention(conn, d2, "2", "Warwick Road")
    dedupe.link(conn)
    assert db.possible_duplicates(conn) == []


# ---- cross-key conflict and document versions ---------------------------------------------------
def test_cross_key_conflict_attaches_to_stronger_key_and_records_pair(conn):
    d1, d2, d3 = doc(conn, "a", council="cockburn"), doc(conn, "b", council="cockburn"), doc(conn, "c", council="cockburn", date="2026-12-01")
    seg = dict(road_name="warton road", from_road="jandakot road", to_road="nicholson road")
    by_seg = mention(conn, d1, "1", "Warton Rd – Jandakot Rd to Nicholson Rd", **seg)
    by_tender = mention(conn, d2, "2", "Tender 7/2026 resurfacing", tender_no="7/2026")
    both = mention(conn, d3, "3", "Tender 7/2026 Warton Road resurfacing", tender_no="7/2026", **seg)
    dedupe.link(conn)
    ps, _ = project_of(conn, by_seg)
    pt, _ = project_of(conn, by_tender)
    pb, lb = project_of(conn, both)
    assert pb.project_id == ps.project_id and lb.link_method == "segment"    # segment outranks tender
    (pair,) = db.possible_duplicates(conn)
    assert {pair["project_a"], pair["project_b"]} == {ps.project_id, pt.project_id} and pair["method"] == "cross_key"
    assert db.get_project(conn, pt.project_id).review_needed


def test_new_version_of_same_document_links_by_name(conn):
    key = "https://www.melvillecity.com.au/our-city/about-our-city/capital-works-program"
    v1 = doc(conn, "a", council="melville", date=None, key=key, retrieved="2026-11-01T00:00:00+00:00")
    v2 = doc(conn, "b", council="melville", date=None, key=key, retrieved="2026-11-22T00:00:00+00:00")
    m1 = mention(conn, v1, "1", "Morris Buzzacott Reserve car park renewal", value_amount=700000, value_basis="budget_total")
    m2 = mention(conn, v2, "2", "Morris Buzzacott Reserve car park renewal", value_amount=750000, value_basis="budget_total")
    dedupe.link(conn)
    p1, _ = project_of(conn, m1)
    p2, l2 = project_of(conn, m2)
    assert p1.project_id == p2.project_id and l2.link_method == "version"
    assert p1.latest_value == 750000 and db.possible_duplicates(conn) == []
    assert (p1.first_seen, p1.last_seen) == ("2026-11-01T00:00:00+00:00", "2026-11-22T00:00:00+00:00")


# ---- project state -----------------------------------------------------------------------------
def test_derive_state_stage_override_and_basis_tiebreak(conn):
    p = Project("p", "joondalup", "code:X", "code")
    docs = {d.doc_id: d for d in (doc(conn, "a", date="2026-07-01"), doc(conn, "b", date="2026-11-17"),
                                  doc(conn, "c", date="2027-02-01"))}
    da, dbb, dc = sorted(docs.values(), key=lambda d: d.meeting_date)

    def m(d, anchor, **kw):
        return ProjectMention(make_mention_id(d.doc_id, "t", anchor), d.doc_id, "joondalup", 1, "X", char_start=0,
                              char_end=1, **kw)
    ms = [m(da, "1", stage="adopted_budget", value_amount=100.0, value_basis="budget_total"),
          m(dbb, "2", stage="tender_awarded", value_amount=90.0, value_basis="contract_award"),
          m(dbb, "3", value_amount=95.0, value_basis="budget_total")]
    out = dedupe.derive_state(p, ms, docs, duplicate=False)
    assert (out.latest_stage, out.latest_value, out.latest_value_basis) == ("tender_awarded", 90.0, "contract_award")
    out = dedupe.derive_state(p, ms + [m(da, "4", stage="deferred")], docs, duplicate=False)
    assert out.latest_stage == "tender_awarded"               # older deferral does not override
    out = dedupe.derive_state(p, ms + [m(dc, "5", stage="deferred")], docs, duplicate=True)
    assert out.latest_stage == "deferred" and out.review_needed and "possible_duplicate" in out.flags


def test_link_cli(tmp_path, capsys):
    path = tmp_path / "r.sqlite"
    conn = db.connect(path)
    d = doc(conn, "a")
    mention(conn, d, "1", "Warwick Road", council_project_code="RPR3542")
    conn.close()
    assert cli.main(["link", "--db", str(path)]) == 0
    assert "linked 1 mention(s) (singleton=1)" in capsys.readouterr().out
    assert cli.main(["link", "--db", str(path)]) == 0
    assert "linked 0 mention(s)" in capsys.readouterr().out


def test_link_over_extractor_output_is_idempotent(tmp_path):
    """Melville CWP HTML + the synthetic Joondalup schedule, ingested as two budget versions, then linked twice."""
    from pathlib import Path

    from radar.ingest import extract_document
    from tests.test_extract_joondalup import URL as JURL, build_schedule_pdf

    conn = db.connect(tmp_path / "r.sqlite")
    fix = Path(__file__).parent / "fixtures" / "melville_cwp_snippet.html"
    extract_document(conn, fix, council_id="melville", url="https://www.melvillecity.com.au/cwp",
                     extractor="melville_cwp", doc_type="capital_program", content_type="text/html",
                     financial_year="2026-27", cache_dir=tmp_path / "c")
    pdf = build_schedule_pdf(tmp_path / "s.pdf")
    extract_document(conn, pdf, council_id="joondalup", url=JURL, extractor="joondalup_budget", doc_type="budget",
                     financial_year="2026-27", cache_dir=tmp_path / "c")
    import pymupdf

    with pymupdf.open(pdf) as d:                                    # same URL, new text -> version 2
        d[0].insert_text((900, 30), "Revised", fontsize=8)
        d.save(tmp_path / "s2.pdf")
    extract_document(conn, tmp_path / "s2.pdf", council_id="joondalup", url=JURL, extractor="joondalup_budget", doc_type="budget",
                     financial_year="2026-27", cache_dir=tmp_path / "c")
    rep = dedupe.link(conn)
    joondalup = db.all_projects(conn, "joondalup")
    assert len(joondalup) == 6 and all(p.dedupe_key.startswith("code:") for p in joondalup)   # 12 mentions -> 6
    assert rep.linked["code"] == 6
    rpr = next(p for p in joondalup if p.dedupe_key == "code:RPR3542")
    assert (rpr.latest_value, rpr.latest_stage, rpr.suburb) == (406364, "adopted_budget", "Warwick")
    first = snapshot(conn)
    assert sum(dedupe.link(conn).linked.values()) == 0 and snapshot(conn) == first
