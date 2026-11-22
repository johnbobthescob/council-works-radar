"""M5: trade matcher rules and gold-set metric."""

import pytest

from radar import trades as T


def codes(name, desc=None, loc=None, **kw):
    return [lab.code for lab in T.default_matcher().match(name, desc, loc, **kw)]


def labels(name, desc=None, loc=None, **kw):
    return {lab.code: lab for lab in T.default_matcher().match(name, desc, loc, **kw)}


def test_tennis_court_resurfacing_is_non_civil():
    got = labels("Tennis court resurfacing")
    assert list(got) == ["NON_CIVIL"]
    assert got["NON_CIVIL"].evidence.startswith("neg:")      # records why ROAD_RESURF was overridden
    assert "ROAD_RESURF" not in got


def test_road_named_court_is_still_resurfacing():
    assert codes("Smith Court resurfacing") == ["ROAD_RESURF"]


def test_code_prefix_prior():
    lab = labels("Warwick Road (westbound)", council="joondalup", project_code="RPR3542")
    assert lab["ROAD_RESURF"].evidence == "prefix:RPR" and lab["ROAD_RESURF"].method == "rule"
    assert codes("Eddystone Ave", council="joondalup", project_code="RDC2032")[0] == "ROAD_CONST"
    # unknown council / code format: no prior
    assert codes("Warwick Road", council="cockburn", project_code="RPR3542") == ["UNKNOWN"]
    assert codes("Warwick Road", council="joondalup", project_code="4C.18") == ["UNKNOWN"]


def test_road_upgrade_implies_kerbing_drainage_linemarking():
    lab = labels("Lakeside Drive duplication")
    assert set(lab) == {"ROAD_CONST", "KERBING", "DRAINAGE", "LINE_MARK"}
    assert lab["ROAD_CONST"].method == "rule"
    for implied in ("KERBING", "DRAINAGE", "LINE_MARK"):
        assert lab[implied].method == "rule_implied" and lab[implied].evidence == "implied:ROAD_CONST"


def test_explicit_label_beats_implied():
    lab = labels("Road upgrade including new kerbing")
    assert lab["KERBING"].method == "rule" and lab["DRAINAGE"].method == "rule_implied"


def test_evidence_records_keyword():
    lab = labels("Alvan Street footpath")
    assert lab["FOOTPATH"].evidence == "kw:\\bfootpath"


@pytest.mark.parametrize("text,expected", [
    ("Water pipe replacement", ["UNKNOWN"]),
    ("Stormwater pipe renewal", ["DRAINAGE"]),
    ("Park path lighting", ["UNKNOWN"]),
    ("Dual use path renewal", ["FOOTPATH"]),
    ("Landscaping of reserve", ["UNKNOWN"]),
    ("Landscaping of road verge", ["STREETSCAPE"]),
])
def test_context_required(text, expected):
    assert codes(text) == expected


def test_negative_must_be_nearby():
    far = "Road resurfacing of Smith Road " + "x" * 100 + " near the tennis club"
    assert codes(far)[0] == "ROAD_RESURF"
    assert codes("Tennis club access road resurfacing") == ["NON_CIVIL"]


def test_non_civil_dropped_when_civil_present_but_override_kept():
    assert codes("Building and footpath works") == ["FOOTPATH"]             # kw NON_CIVIL dropped
    got = codes("Tennis court resurfacing and car park")
    assert "CARPARK" in got and "NON_CIVIL" in got                           # override stays


def test_it_upgrade_is_not_road_construction():
    assert codes("IT systems upgrade") == ["NON_CIVIL"]


def test_description_and_location_are_used():
    assert codes("Alvan Street", "Resurfacing of the road", "Alvan Street, Mount Pleasant") == ["ROAD_RESURF"]


def test_unknown_when_nothing_fires():
    got = T.default_matcher().match("Misc works")
    assert [(g.code, g.evidence) for g in got] == [("UNKNOWN", "no_rule")]


def test_match_trades_returns_dicts_and_fallback_hook():
    d = T.match_trades("Footpaths Program")
    assert d == [{"code": "FOOTPATH", "method": "rule", "evidence": "kw:\\bfootpath"}]
    assert T.classify_fallback("anything") == []


def test_label_mention_integrates_with_model():
    from radar import models as m
    mt = m.ProjectMention(mention_id="a", doc_id="d", council_id="joondalup", page=1, char_start=0, char_end=1,
                          project_name_raw="Warwick Road", council_project_code="RPR3542")
    assert T.label_mention(mt)[0]["code"] == "ROAD_RESURF"


def test_gold_set_precision_recall():
    rows = T.load_gold()
    assert len(rows) >= 30
    metrics = T.evaluate_gold(rows)
    assert {"ROAD_RESURF", "FOOTPATH", "DRAINAGE", "NON_CIVIL", "INTERSECTION", "UNKNOWN"} <= set(metrics)
    for code, m in metrics.items():
        assert m["precision"] >= 0.85 and m["recall"] >= 0.85, (code, m)


def test_trades_report_cli(tmp_path, capsys):
    from radar import cli, db, models as m
    path = tmp_path / "r.sqlite"
    conn = db.connect(path)
    doc = m.SourceDocument(doc_id="d", doc_key="k", council_id="joondalup", source_url="http://x")
    db.upsert_source_document(conn, doc)
    for i, (name, labs) in enumerate([("Alvan Street footpath", ["FOOTPATH"]), ("Misc works", ["UNKNOWN"]),
                                      ("Misc works", ["UNKNOWN"])]):
        db.upsert_mention(conn, m.ProjectMention(
            mention_id=f"m{i}", doc_id="d", council_id="joondalup", page=1, char_start=0, char_end=1,
            project_name_raw=name, trade_labels=[{"code": c, "method": "rule", "evidence": "x"} for c in labs]))
    conn.close()
    assert cli.main(["trades-report", "--db", str(path)]) == 0
    out = capsys.readouterr().out
    assert "FOOTPATH" in out and "2  Misc works" in out


def test_trades_report_empty_db_and_gold(tmp_path, capsys):
    from radar import cli
    assert cli.main(["trades-report", "--db", str(tmp_path / "e.sqlite")]) == 0
    assert "no mentions" in capsys.readouterr().out
    assert cli.main(["trades-report", "--gold"]) == 0
    assert "ROAD_RESURF" in capsys.readouterr().out
