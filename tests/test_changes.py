"""M13: change tracking snapshots, diffs, thresholds, baseline and idempotency (offline)."""

from datetime import datetime, timezone

import pytest

from radar import changes, db, dedupe
from radar.models import ProjectMention, SourceDocument, make_mention_id

RESURF = [{"code": "ROAD_RESURF", "method": "rule", "evidence": "kw:resurfac"}]
T1 = datetime(2026, 11, 16, 1, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 11, 23, 1, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    return db.connect(":memory:")


def doc(conn, name, date, retrieved):
    d = SourceDocument(doc_id=(name * 64)[:64], doc_key=f"https://example.org/{name}", council_id="joondalup",
                       source_url=f"https://example.org/{name}", doc_type="budget", meeting_date=date, retrieved_at=retrieved)
    db.upsert_source_document(conn, d)
    return d


def mention(conn, d, anchor, name, **kw):
    kw.setdefault("trade_labels", RESURF)
    m = ProjectMention(mention_id=make_mention_id(d.doc_id, "test", anchor), doc_id=d.doc_id, council_id=d.council_id,
                       page=1, char_start=0, char_end=1, project_name_raw=name, extractor="test", **kw)
    db.upsert_mention(conn, m)
    return m


def run_cycle(conn, when):
    run = changes.start_run(conn, ["joondalup"], now=when)
    dedupe.link(conn)
    report = changes.record_changes(conn, run, now=when)
    changes.finish_run(conn, run, now=when)
    return run, report


def by_code(conn, report, code):
    pid = conn.execute("SELECT project_id FROM project WHERE dedupe_key = ?", (f"code:{code}",)).fetchone()[0]
    return {e.event_type: e for e in report.events if e.project_id == pid}


@pytest.fixture
def baseline(conn):
    d = doc(conn, "a", "2026-07-01", "2026-11-16T00:00:00+00:00")
    mention(conn, d, "1", "Warwick Road", council_project_code="RPR1", value_amount=400000, value_basis="budget_total",
            stage="adopted_budget", road_name="Warwick Road", financial_year="2026-27")
    mention(conn, d, "2", "Small Job", council_project_code="RPR2", value_amount=100000, value_basis="budget_total",
            stage="adopted_budget")
    mention(conn, d, "3", "Steady Job", council_project_code="RPR3", value_amount=50000, value_basis="budget_total",
            stage="adopted_budget")
    mention(conn, d, "4", "Moving Job", council_project_code="RPR4", value_amount=50000, value_basis="budget_total",
            stage="adopted_budget")
    mention(conn, d, "5", "Doomed Job", council_project_code="RPR5", value_amount=20000, stage="adopted_budget")
    return d


def test_baseline_run_is_all_new(conn, baseline):
    run, report = run_cycle(conn, T1)
    assert report.baseline and len(report.events) == 5 and report.by_type == {"new": 5}
    assert all(e.mention_id and e.run_id == run.run_id for e in report.events)
    stored = db.get_run(conn, run.run_id)
    assert stored.iso_week == "2026-W47" and stored.finished_at
    assert stored.counts["baseline"] == 1 and stored.counts["events_new"] == 5 and stored.counts["projects"] == 5


def test_second_run_emits_expected_event_types_and_deltas(conn, baseline):
    run_cycle(conn, T1)
    review = doc(conn, "b", "2027-02-24", "2026-11-23T00:00:00+00:00")
    mention(conn, review, "1", "Warwick Road resurfacing", council_project_code="RPR1", value_amount=456000,
            value_basis="budget_total", stage="tender_awarded", suburb="Warwick", road_name="Warwick Road")
    mention(conn, review, "2", "Small Job", council_project_code="RPR2", value_amount=105000, value_basis="budget_total")
    mention(conn, review, "3", "Steady Job", council_project_code="RPR3")
    mention(conn, review, "5", "Doomed Job", council_project_code="RPR5", stage="deleted", uncertainty_flags=["carry_forward"])
    mention(conn, review, "6", "Brand New Job", council_project_code="RPR6", value_amount=75000)
    _run, report = run_cycle(conn, T2)
    assert not report.baseline

    p1 = by_code(conn, report, "RPR1")
    assert set(p1) == {"value_changed", "stage_advanced", "location_refined"}
    assert p1["value_changed"].old == {"value": 400000, "basis": "budget_total"}
    assert p1["value_changed"].new["value"] == 456000 and p1["value_changed"].new["delta"] == 56000
    assert p1["value_changed"].new["pct"] == 0.14
    assert p1["stage_advanced"].old == {"stage": "adopted_budget"} and p1["stage_advanced"].new == {"stage": "tender_awarded"}
    assert p1["location_refined"].old == {"suburb": None} and p1["location_refined"].new == {"suburb": "Warwick"}
    # the evidence for each event is the mention that carries the new fact
    assert db.get_mention(conn, p1["value_changed"].mention_id).value_amount == 456000
    assert db.get_mention(conn, p1["stage_advanced"].mention_id).stage == "tender_awarded"

    assert by_code(conn, report, "RPR2") .keys() == {"source_added"}       # +$5k is below the $10k floor
    assert by_code(conn, report, "RPR3").keys() == {"source_added"}
    assert by_code(conn, report, "RPR4") == {}                              # untouched: no event at all
    p5 = by_code(conn, report, "RPR5")
    assert set(p5) == {"deleted"} and p5["deleted"].new == {"stage": "deleted", "carry_forward": True}
    assert set(by_code(conn, report, "RPR6")) == {"new"}
    assert report.by_type == {"value_changed": 1, "stage_advanced": 1, "location_refined": 1, "source_added": 2,
                              "deleted": 1, "new": 1}
    assert db.get_run(conn, report.run_id).counts["baseline"] == 0


def test_record_changes_is_idempotent_per_run(conn, baseline):
    run, first = run_cycle(conn, T1)
    ids = sorted(e.event_id for e in first.events)
    again = changes.record_changes(conn, run, now=T1)
    assert again.baseline and sorted(e.event_id for e in again.events) == ids
    assert conn.execute("SELECT COUNT(*) FROM change_event").fetchone()[0] == 5
    # a third run with nothing new is quiet
    _r, quiet = run_cycle(conn, T2)
    assert quiet.events == [] and not quiet.baseline


@pytest.mark.parametrize("old,new,expected", [
    (400000, 420000, True),        # exactly 5% and >= $10k
    (400000, 419999, False),       # just under 5%
    (100000, 109999, False),       # 5% rule < $10k floor: floor wins
    (100000, 110000, True),        # exactly $10k
    (100000, 90000, True),         # decreases count
    (2_000_000, 2_099_000, False), # 4.95%
    (2_000_000, 2_100_000, True),
    (None, 50000, True),           # null -> value: old treated as 0
    (None, 5000, False),
    (50000, None, False),          # value disappearing is not a delta
    (50000, 50000, False),
])
def test_value_threshold(old, new, expected):
    assert changes.value_change_is_material(old, new) is expected


def test_diff_stage_rules():
    def d(old, new, flags=()):
        before = {"latest_stage": old, "source_count": 1}
        after = {"latest_stage": new, "source_count": 1, "flags": list(flags)}
        return [c.event_type for c in changes.diff_project("p", before, after)]

    assert d("adopted_budget", "tender_awarded") == ["stage_advanced"]
    assert d("tender_awarded", "adopted_budget") == []                     # moving backwards is not an advance
    assert d("adopted_budget", "deferred") == ["deferred"]
    assert d("deferred", "adopted_budget") == ["stage_advanced"]           # revived
    assert d("adopted_budget", "adopted_budget") == []
    assert d(None, "proposed_budget") == ["stage_advanced"]
    assert d("adopted_budget", "deleted", ["carry_forward"]) == ["deleted"]


def test_previous_snapshot_uses_latest_other_run(conn, baseline):
    r1, _ = run_cycle(conn, T1)
    r2, _ = run_cycle(conn, T2)
    assert changes.previous_snapshot(conn, r2.run_id).keys() == changes.previous_snapshot(conn, r1.run_id).keys()
    assert changes.previous_snapshot(conn, "none-yet") is not None
    fresh = db.connect(":memory:")
    assert changes.previous_snapshot(fresh, "x") is None
    assert changes.iso_week(datetime(2027, 1, 3, 20, 0, tzinfo=timezone.utc)) == "2027-W01"   # Perth (UTC+8) is already Mon 4 Jan
