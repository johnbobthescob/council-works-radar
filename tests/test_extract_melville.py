"""M8: Melville Capital Works Program extractor on a hand-built HTML snippet (offline)."""

from pathlib import Path

import pytest

from radar import db
from radar.extract import get_extractor, melville_cwp
from radar.ingest import extract_document
from radar.models import SourceDocument
from radar.text import extract_html

FIX = Path(__file__).parent / "fixtures" / "melville_cwp_snippet.html"
URL = "https://www.melvillecity.com.au/our-city/about-our-city/capital-works-program"


@pytest.fixture
def source():
    return SourceDocument(doc_id="d" * 64, doc_key=URL, council_id="melville", source_url=URL,
                          doc_type="capital_program", financial_year=None)


@pytest.fixture
def mentions(source):
    return melville_cwp.extract(extract_html(FIX), source, now="2026-11-22T00:00:00+00:00")


def by_name(ms, start):
    return next(m for m in ms if m.project_name_raw.startswith(start))


def test_projects_found_with_values(mentions):
    assert [m.project_name_raw for m in mentions] == [
        "Ardross Street and Munro Road Roundabout Installation", "Norma Road and McCoy Street Roundabout",
        "Leach Highway Path Renewal", "Morris Buzzacott Reserve car park renewal", "Applecross Library roof replacement"]
    assert [m.value_amount for m in mentions] == [700000, 550000, 1200000, 700000, 250000]
    assert all(m.value_basis == "current_year" and m.stage == "adopted_budget" for m in mentions)
    assert all(m.financial_year == "2026-27" for m in mentions)          # from the h1


def test_funding_refs(mentions):
    assert by_name(mentions, "Ardross").funding_refs == ["BlackSpot_State", "Grant"]
    assert "BlackSpot_Federal" in by_name(mentions, "Norma").funding_refs
    assert by_name(mentions, "Leach").funding_refs == ["Reserve", "Municipal"]   # from the next block's description
    assert by_name(mentions, "Morris").funding_refs == []


def test_description_joined_from_next_block(mentions):
    leach = by_name(mentions, "Leach")
    assert "Shared path renewal" in leach.quote and leach.value_amount == 1200000
    assert len(mentions) == 5                        # the joined block is not counted as a project again


def test_gst_and_flags(mentions):
    lib = by_name(mentions, "Applecross Library")
    assert lib.value_gst == "ex" and "gst_unknown" not in lib.uncertainty_flags
    assert by_name(mentions, "Ardross").value_gst == "unknown" and "gst_unknown" in by_name(mentions, "Ardross").uncertainty_flags


def test_location_fields(mentions):
    assert by_name(mentions, "Ardross").location_raw is not None            # intersection
    assert by_name(mentions, "Applecross").suburb == "Applecross"


def test_provenance_and_metadata(mentions, source):
    text = extract_html(FIX).full_text
    for m in mentions:
        assert m.page == 1 and m.doc_id == source.doc_id and m.council_id == "melville"
        assert m.char_start is not None and m.char_end > m.char_start
        assert m.project_name_raw in text[m.char_start:m.char_end]
        assert m.extractor == "melville_cwp" and m.extractor_version == "1"
        assert m.extracted_at == "2026-11-22T00:00:00+00:00"
        assert 0 < len(m.quote) <= 300 and 0 <= m.confidence <= 1
        assert m.trade_labels and all("code" in t and "evidence" in t for t in m.trade_labels)


def test_trade_labels(mentions):
    codes = {m.project_name_raw: {t["code"] for t in m.trade_labels} for m in mentions}
    assert "INTERSECTION" in codes["Norma Road and McCoy Street Roundabout"]
    assert "NON_CIVIL" in codes["Applecross Library roof replacement"]


def test_ids_deterministic_and_unique(source, mentions):
    again = melville_cwp.extract(extract_html(FIX), source)
    assert [m.mention_id for m in again] == [m.mention_id for m in mentions]
    assert len({m.mention_id for m in mentions}) == 5


def test_registry():
    assert get_extractor("melville_cwp") is melville_cwp.extract
    with pytest.raises(KeyError):
        get_extractor("blackspot")


def test_extract_document_end_to_end_offline(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite")
    doc, n = extract_document(conn, FIX, council_id="melville", url=URL, extractor="melville_cwp",
                              doc_type="capital_program", content_type="text/html", cache_dir=tmp_path / "cache")
    assert n == 5 and doc.extract_status == "ok" and doc.financial_year == "2026-27"
    rows = db.mentions_for_document(conn, doc.doc_id)
    assert len(rows) == 5
    # idempotent
    _, n2 = extract_document(conn, FIX, council_id="melville", url=URL, extractor="melville_cwp",
                             doc_type="capital_program", content_type="text/html", cache_dir=tmp_path / "cache")
    assert n2 == 5 and len(db.mentions_for_document(conn, doc.doc_id)) == 5


def test_refetch_with_new_bytes_but_same_text_is_not_a_new_version(tmp_path):
    conn = db.connect(":memory:")
    kw = dict(council_id="melville", url=URL, extractor="melville_cwp", doc_type="capital_program",
              content_type="text/html", cache_dir=tmp_path / "c")
    d1, n1 = extract_document(conn, FIX, **kw)
    copy = tmp_path / "page.html"                                   # e.g. a per-request token in a <script>
    copy.write_bytes(FIX.read_bytes() + b"\n<script>var token='abc123';</script>\n")
    d2, n2 = extract_document(conn, copy, **kw)
    assert d2.doc_id == d1.doc_id and d2.version_no == 1 and n2 == n1
    assert len(db.all_source_documents(conn)) == 1
