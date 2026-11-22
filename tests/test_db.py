"""M1: schema creation, round-trips and idempotent upserts."""

import dataclasses

import pytest

from radar import db, models as m


def _doc(**kw):
    base = dict(
        doc_id=m.sha256_bytes(b"hello"), doc_key=m.canonical_url("https://API.Example.wa.gov.au/files/a b.pdf?v=1"),
        council_id="joondalup", source_url="https://api.example.wa.gov.au/files/a b.pdf?v=1",
        doc_type="budget", financial_year="2026-27", bytes=5, retrieved_at="2026-11-22T10:00:00+08:00",
    )
    base.update(kw)
    return m.SourceDocument(**base)


def _mention(doc, **kw):
    base = dict(
        mention_id=m.make_mention_id(doc.doc_id, "joondalup_budget", "RPR3542"), doc_id=doc.doc_id,
        council_id="joondalup", page=41, char_start=10, char_end=80, project_name_raw="Warwick Road",
        council_project_code="RPR3542", value_amount=406364.0, value_basis="budget_total",
        trade_labels=[{"code": "ROAD_RESURF", "method": "rule", "evidence": "prefix:RPR"}],
        funding_split={"municipal": 135455.0, "grants_new": 270909.0},
        uncertainty_flags=["gst_unknown"], quote="Resurfacing and rehabilitation works",
    )
    base.update(kw)
    return m.ProjectMention(**base)


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def test_fresh_db_has_schema_version_and_is_idempotent(conn, tmp_path):
    assert db.schema_version(conn) == db.SCHEMA_VERSION
    db.migrate(conn)
    assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1
    p = tmp_path / "sub" / "radar.sqlite"
    db.connect(p).close()
    db.connect(p).close()  # reopening an existing file is fine
    assert p.exists()


def test_source_document_roundtrip(conn):
    d = _doc()
    db.upsert_source_document(conn, d)
    assert db.get_source_document(conn, d.doc_id) == d
    assert db.latest_document_for_key(conn, d.doc_key).doc_id == d.doc_id
    assert db.get_source_document(conn, "nope") is None


def test_canonical_url_strips_cache_busters():
    a = m.canonical_url("HTTPS://Api.Example.wa.gov.au/files/x.pdf?v=1&language=en#p=2")
    assert a == "https://api.example.wa.gov.au/files/x.pdf"
    assert m.canonical_url("https://h/a b.pdf?id=3&v=2") == "https://h/a%20b.pdf?id=3"


def test_mention_roundtrip_preserves_json_fields(conn):
    d = _doc()
    db.upsert_source_document(conn, d)
    mt = _mention(d)
    db.upsert_mention(conn, mt)
    got = db.get_mention(conn, mt.mention_id)
    assert got == mt
    assert got.trade_labels[0]["evidence"] == "prefix:RPR"
    assert got.funding_split["municipal"] == 135455.0
    assert db.mentions_for_document(conn, d.doc_id) == [mt]


def test_upsert_mention_is_idempotent_and_updates(conn):
    d = _doc()
    db.upsert_source_document(conn, d)
    mt = _mention(d)
    db.upsert_mention(conn, mt)
    db.upsert_mention(conn, mt)
    assert db.count(conn, m.ProjectMention) == 1
    db.upsert_mention(conn, dataclasses.replace(mt, value_amount=500000.0))
    assert db.count(conn, m.ProjectMention) == 1
    assert db.get_mention(conn, mt.mention_id).value_amount == 500000.0


def test_mention_id_is_deterministic_and_distinct():
    a = m.make_mention_id("d1", "x", "anchor")
    assert a == m.make_mention_id("d1", "x", "anchor")
    assert a != m.make_mention_id("d1", "x", "other")
    assert a != m.make_mention_id("d2", "x", "anchor")


def test_mention_requires_provenance():
    with pytest.raises(ValueError):
        m.ProjectMention(mention_id="a", doc_id="d", council_id="c", page=1, project_name_raw="x")
    with pytest.raises(ValueError):
        m.ProjectMention(mention_id="a", doc_id="d", council_id="c", page=0, project_name_raw="x", bbox=[0, 0, 1, 1])
    ok = m.ProjectMention(mention_id="a", doc_id="d", council_id="c", page=1, project_name_raw="x", bbox=[0, 0, 1, 1])
    assert ok.bbox == [0, 0, 1, 1]


def test_quote_capped_at_300():
    mt = m.ProjectMention(mention_id="a", doc_id="d", council_id="c", page=1, project_name_raw="x",
                          char_start=0, char_end=1, quote="q" * 500)
    assert len(mt.quote) <= 300


def test_fetch_log_project_link_event_run(conn):
    d = _doc()
    db.upsert_source_document(conn, d)
    mt = _mention(d)
    db.upsert_mention(conn, mt)
    db.insert_fetch_log(conn, m.FetchLogEntry(ts="t", method="GET", url="http://h/x", host="h", status=200,
                                              bytes=3, cache_hit=True))
    assert db.fetch_log_rows(conn)[0].cache_hit is True
    pid = m.make_project_id("joondalup", "code:RPR3542")
    p = m.Project(project_id=pid, council_id="joondalup", dedupe_key="RPR3542", dedupe_key_type="code",
                  trade_codes=["ROAD_RESURF"], review_needed=True)
    db.upsert_project(conn, p)
    db.upsert_link(conn, m.ProjectMentionLink(project_id=pid, mention_id=mt.mention_id, link_method="code"))
    assert db.get_project(conn, pid) == p
    ev = m.ChangeEvent(event_id=m.make_event_id("r1", pid, "new"), run_id="r1", project_id=pid, event_type="new",
                       new={"value": 1})
    db.upsert_change_event(conn, ev)
    db.upsert_change_event(conn, ev)
    assert db.count(conn, m.ChangeEvent) == 1
    r = m.Run(run_id="r1", started_at="t", councils=["joondalup"], counts={"docs": 1})
    db.upsert_run(conn, r)
    assert db.get_run(conn, "r1") == r
