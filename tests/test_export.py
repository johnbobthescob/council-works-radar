"""M15: UI data export (PLAN.md §7.2) on a fixture DB (offline)."""

import json
from datetime import datetime, timezone

import pytest

from radar import changes, cli, db, dedupe, export
from radar.models import ProjectMention, SourceDocument, make_mention_id

RESURF = [{"code": "ROAD_RESURF", "method": "rule", "evidence": "kw:resurfac"},
          {"code": "KERBING", "method": "rule_implied", "evidence": "implied:ROAD_RESURF"}]
DRAIN = [{"code": "DRAINAGE", "method": "rule", "evidence": "kw:drainage"}]
T1 = datetime(2026, 11, 17, 1, 0, tzinfo=timezone.utc)


def doc(conn, name, council, url, doc_type="budget", **kw):
    d = SourceDocument(doc_id=(name * 64)[:64], doc_key=url, council_id=council, source_url=url, doc_type=doc_type,
                       retrieved_at="2026-11-17T01:00:00+00:00", content_type="application/pdf", **kw)
    db.upsert_source_document(conn, d)
    return d


def mention(conn, d, anchor, name, page=1, **kw):
    kw.setdefault("trade_labels", RESURF)
    kw.setdefault("quote", "A short factual quote.")
    m = ProjectMention(mention_id=make_mention_id(d.doc_id, "test", anchor), doc_id=d.doc_id, council_id=d.council_id,
                       page=page, char_start=0, char_end=1, project_name_raw=name, extractor="test", **kw)
    db.upsert_mention(conn, m)
    return m


def cycle(conn, when=T1):
    run = changes.start_run(conn, ["joondalup", "melville"], now=when)
    dedupe.link(conn)
    changes.record_changes(conn, run, now=when)
    changes.finish_run(conn, run, now=when)


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    jd = doc(c, "a", "joondalup", "https://www.joondalup.wa.gov.au/budget.pdf", title="Budget", financial_year="2026-27")
    mention(c, jd, "1", "Warwick Road (westbound)", page=41, council_project_code="RPR3542", value_amount=406364,
            value_basis="budget_total", stage="adopted_budget", suburb="Warwick", financial_year="2026-27",
            quote="x" * 1000, confidence=0.55)
    mention(c, jd, "2", "Hepburn Avenue Drainage", page=42, council_project_code="RDC1", value_amount=90000,
            value_basis="budget_total", trade_labels=DRAIN, confidence=0.8)
    return c


def exported(conn, tmp_path, **kw):
    ui = export.export_ui(conn, tmp_path, now=T1, **kw)
    return ui, (ui / "data.js").read_text(encoding="utf-8")


def test_export_writes_files_and_roundtrips(conn, tmp_path):
    cycle(conn)
    ui, text = exported(conn, tmp_path)
    assert ui == tmp_path / "ui"
    for name in (*export.UI_FILES, "data.js"):
        assert (ui / name).is_file()
    assert text.startswith(export.DATA_PREFIX) == text.startswith("window.RADAR_DATA = ")
    assert text.startswith("window.RADAR_DATA = ")
    manual = json.loads(text.strip()[len("window.RADAR_DATA = "):].removesuffix(";"))
    assert manual == export.parse_data_js(text) and isinstance(manual, dict)


def test_payload_shape_and_baseline(conn, tmp_path):
    cycle(conn)
    p = export.build_data(conn, now=T1)
    assert {"generated_at", "iso_week", "baseline", "councils", "trades", "footer", "projects"} <= set(p)
    assert p["baseline"] is True and p["iso_week"] == "2026-W47"
    assert [c["id"] for c in p["councils"]] == ["joondalup"]       # melville has no projects
    assert len(p["projects"]) == 2
    for proj in p["projects"]:
        assert any(e["type"] == "new" for e in proj["history"])
        assert proj["last_change"] and proj["last_change"]["type"]


def test_sources_have_urls_and_short_quotes(conn, tmp_path):
    cycle(conn)
    p = export.parse_data_js(exported(conn, tmp_path)[1])
    assert p["projects"]
    for proj in p["projects"]:
        assert proj["sources"]
        for s in proj["sources"]:
            assert s["url"].startswith(("http://", "https://")) and s["retrieved_at"]
            assert len(s["quote"] or "") <= 300
    warwick = next(x for x in p["projects"] if "Warwick" in x["name"])
    assert warwick["sources"][0]["quote"]


def test_size_budget(conn, tmp_path, monkeypatch):
    cycle(conn)
    _, text = exported(conn, tmp_path)
    assert len(text.encode()) < export.MAX_BYTES == 5 * 1024 * 1024
    monkeypatch.setattr(export, "MAX_BYTES", 100)
    with pytest.raises(ValueError, match="budget"):
        export.export_ui(conn, tmp_path / "other", now=T1)


def test_script_close_escaped(conn, tmp_path):
    d = doc(conn, "e", "melville", "https://www.melvillecity.com.au/x.pdf")
    mention(conn, d, "1", "Evil </script><b>x</b> Road", trade_labels=DRAIN)
    cycle(conn)
    _, text = exported(conn, tmp_path)
    assert "</script>" not in text and "<\\/script>" in text
    names = [x["name"] for x in export.parse_data_js(text)["projects"]]
    assert any("</script>" in n for n in names)


def test_implied_trades_separate(conn, tmp_path):
    cycle(conn)
    p = export.build_data(conn, now=T1)
    warwick = next(x for x in p["projects"] if "Warwick" in x["name"])
    assert "ROAD_RESURF" in warwick["trades"] and "KERBING" not in warwick["trades"]
    assert warwick["implied"] == ["KERBING"]


def test_index_html_ids_and_script_order():
    html = (export.UI_SOURCE_DIR / "index.html").read_text(encoding="utf-8")
    ids = ("search filter-council filter-trade include-implied filter-stage filter-fy value-min value-max "
           "filter-confidence filter-weeks filter-flags results results-body count-shown count-total "
           "value-total download-csv").split()
    for i in ids:
        assert f'id="{i}"' in html, i
    assert html.index('src="data.js"') < html.index('src="app.js"')


def test_empty_db_exports_valid_data_js(tmp_path):
    c = db.connect(":memory:")
    _, text = exported(c, tmp_path)
    p = export.parse_data_js(text)
    assert p["projects"] == [] and p["councils"] == [] and p["baseline"] is False


def test_cli_export(conn, tmp_path):
    cycle(conn)
    path = tmp_path / "t.sqlite"
    disk = db.connect(path)
    conn.backup(disk)
    disk.close()
    assert cli.main(["export", "--db", str(path), "--out", str(tmp_path)]) == 0
    assert export.parse_data_js((tmp_path / "ui" / "data.js").read_text(encoding="utf-8"))["projects"]
