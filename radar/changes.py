"""Change tracking (PLAN.md §5.3): per-run project snapshots, diffs and ``change_event`` rows.

Flow: ``start_run`` -> (fetch / extract / link) -> ``record_changes`` -> ``finish_run``.

``record_changes`` takes the *current* derived state of every project, diffs it against the snapshot stored by
the previous run (the latest run other than this one) and writes events plus a new snapshot for this run.
With no earlier snapshot it is the **baseline** run: every project is ``new``. It is idempotent per run id:
event ids are deterministic and the run's snapshot is replaced, so re-running it changes nothing.

Events (at most one of each type per project per run):
  new              the project had no snapshot entry
  value_changed    |delta| >= max($10,000, 5% of the old value); null -> value counts (old treated as 0)
  stage_advanced   stage moved forward in the pipeline order (no stage / deferred / deleted count as before it)
  deferred/deleted the stage became deferred or deleted; ``carry_forward`` is added when that flag is present
  location_refined road / from / to / suburb went from null to a value
  source_added     more source documents mention the project and nothing else about it changed
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from radar import db
from radar.dedupe import OVERRIDE_STAGES, STAGE_ORDER
from radar.models import ChangeEvent, Project, Run, make_event_id
from radar.settings import TIMEZONE

VALUE_MIN_DELTA = 10_000.0
VALUE_MIN_PCT = 0.05
LOCATION_FIELDS = ("road_name", "from_road", "to_road", "suburb")
EVENT_TYPES = ("new", "value_changed", "stage_advanced", "deferred", "deleted", "location_refined", "source_added")


def iso_week(when: datetime | None = None) -> str:
    """ISO week label in Perth time, e.g. ``2026-W47``."""
    when = (when or datetime.now(timezone.utc)).astimezone(ZoneInfo(TIMEZONE))
    year, week, _ = when.isocalendar()
    return f"{year}-W{week:02d}"


# ---- run table ---------------------------------------------------------------------------------
def start_run(conn: sqlite3.Connection, councils: list[str] | None = None, *, now: datetime | None = None) -> Run:
    now = now or datetime.now(timezone.utc)
    run = Run(run_id=f"{now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}",
              started_at=now.isoformat(timespec="seconds"), iso_week=iso_week(now), councils=list(councils or []))
    db.upsert_run(conn, run)
    return run


def finish_run(conn: sqlite3.Connection, run: Run, counts: dict | None = None, errors: list | None = None,
               *, now: datetime | None = None) -> Run:
    run.finished_at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    run.counts = {**run.counts, **(counts or {})}
    run.errors = list(run.errors) + list(errors or [])
    db.upsert_run(conn, run)
    return run


# ---- snapshots ---------------------------------------------------------------------------------
def project_state(project: Project, source_count: int) -> dict:
    return {"latest_value": project.latest_value, "latest_value_basis": project.latest_value_basis,
            "latest_stage": project.latest_stage, "canonical_name": project.canonical_name,
            **{f: getattr(project, f) for f in LOCATION_FIELDS}, "flags": sorted(project.flags or []),
            "source_count": source_count}


def current_state(conn: sqlite3.Connection) -> dict[str, dict]:
    """``project_id -> state`` from the database as it is now."""
    counts = {r[0]: r[1] for r in conn.execute(
        "SELECT l.project_id, COUNT(DISTINCT d.doc_key) FROM project_mention_link l "
        "JOIN project_mention m ON m.mention_id = l.mention_id JOIN source_document d ON d.doc_id = m.doc_id "
        "GROUP BY l.project_id")}       # distinct documents (URLs): a new version of the same doc is not a new source
    return {p.project_id: project_state(p, counts.get(p.project_id, 0)) for p in db.all_projects(conn)}


def previous_snapshot(conn: sqlite3.Connection, run_id: str) -> dict[str, dict] | None:
    """Snapshot of the latest run other than ``run_id`` that stored one; None if there is none (baseline)."""
    row = conn.execute(
        "SELECT s.run_id FROM project_snapshot s LEFT JOIN run r ON r.run_id = s.run_id WHERE s.run_id != ? "
        "GROUP BY s.run_id ORDER BY COALESCE(MAX(r.started_at), ''), s.run_id DESC LIMIT 1", (run_id,)).fetchone()
    if row is None:
        return None
    return {r["project_id"]: json.loads(r["state"]) for r in conn.execute(
        "SELECT project_id, state FROM project_snapshot WHERE run_id = ?", (row["run_id"],))}


def save_snapshot(conn: sqlite3.Connection, run_id: str, state: dict[str, dict]) -> None:
    conn.execute("DELETE FROM project_snapshot WHERE run_id = ?", (run_id,))
    conn.executemany("INSERT INTO project_snapshot (run_id, project_id, state) VALUES (?, ?, ?)",
                     [(run_id, pid, json.dumps(s, sort_keys=True)) for pid, s in sorted(state.items())])
    conn.commit()


# ---- diff --------------------------------------------------------------------------------------
@dataclass
class Change:
    project_id: str
    event_type: str
    old: dict | None = None
    new: dict | None = None


def _stage_rank(stage: str | None) -> int:
    return STAGE_ORDER.index(stage) if stage in STAGE_ORDER else -1


def value_change_is_material(old: float | None, new: float | None) -> bool:
    if new is None or new == old:
        return False
    base = old or 0.0
    return abs(new - base) >= max(VALUE_MIN_DELTA, VALUE_MIN_PCT * abs(base))


def diff_project(project_id: str, before: dict | None, after: dict) -> list[Change]:
    if before is None:
        return [Change(project_id, "new", None, {k: after[k] for k in ("canonical_name", "latest_value", "latest_stage")})]
    out: list[Change] = []
    ov, nv = before.get("latest_value"), after.get("latest_value")
    if value_change_is_material(ov, nv):
        delta = nv - (ov or 0.0)
        pct = round(delta / ov, 4) if ov else None
        out.append(Change(project_id, "value_changed", {"value": ov, "basis": before.get("latest_value_basis")},
                          {"value": nv, "basis": after.get("latest_value_basis"), "delta": round(delta, 2), "pct": pct}))
    os_, ns = before.get("latest_stage"), after.get("latest_stage")
    if ns != os_:
        if ns in OVERRIDE_STAGES:
            carry = "carry_forward" in (after.get("flags") or [])
            out.append(Change(project_id, ns, {"stage": os_}, {"stage": ns, **({"carry_forward": True} if carry else {})}))
        elif ns in STAGE_ORDER and _stage_rank(ns) > _stage_rank(os_):
            out.append(Change(project_id, "stage_advanced", {"stage": os_}, {"stage": ns}))
    refined = [f for f in LOCATION_FIELDS if not before.get(f) and after.get(f)]
    if refined:
        out.append(Change(project_id, "location_refined", {f: None for f in refined}, {f: after[f] for f in refined}))
    if not out and after.get("source_count", 0) > before.get("source_count", 0):
        out.append(Change(project_id, "source_added", {"source_count": before.get("source_count", 0)},
                          {"source_count": after["source_count"]}))
    return out


def diff_states(before: dict[str, dict] | None, after: dict[str, dict]) -> list[Change]:
    """All changes between two ``project_id -> state`` maps. ``before=None`` is the baseline (everything new)."""
    changes: list[Change] = []
    for pid in sorted(after):
        changes += diff_project(pid, (before or {}).get(pid) if before is not None else None, after[pid])
    return changes


# ---- evidence + persistence --------------------------------------------------------------------
def _evidence(conn: sqlite3.Connection, ch: Change) -> str | None:
    """The mention that backs an event: the one carrying the new value / stage, else the newest source."""
    rows = conn.execute(
        "SELECT m.mention_id, m.value_amount, m.stage, m.confidence, d.retrieved_at FROM project_mention_link l "
        "JOIN project_mention m ON m.mention_id = l.mention_id LEFT JOIN source_document d ON d.doc_id = m.doc_id "
        "WHERE l.project_id = ?", (ch.project_id,)).fetchall()
    if not rows:
        return None

    def pick(pred):
        hits = [r for r in rows if pred(r)] or rows
        return max(hits, key=lambda r: (r["retrieved_at"] or "", r["confidence"], r["mention_id"]))["mention_id"]

    new = ch.new or {}
    if ch.event_type == "value_changed":
        return pick(lambda r: r["value_amount"] == new.get("value"))
    if ch.event_type in ("stage_advanced", "deferred", "deleted"):
        return pick(lambda r: r["stage"] == new.get("stage"))
    return pick(lambda r: True)


@dataclass
class ChangeReport:
    run_id: str
    baseline: bool
    events: list[ChangeEvent] = field(default_factory=list)
    projects: int = 0

    @property
    def by_type(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.events:
            out[e.event_type] = out.get(e.event_type, 0) + 1
        return out

    def summary(self) -> str:
        kind = "baseline run" if self.baseline else "incremental run"
        types = ", ".join(f"{k}={v}" for k, v in sorted(self.by_type.items())) or "no changes"
        return f"{kind} {self.run_id}: {self.projects} project(s); events: {types}"


def record_changes(conn: sqlite3.Connection, run: Run, *, now: datetime | None = None) -> ChangeReport:
    """Diff the current project state against the previous run's snapshot; store events and this run's snapshot."""
    after = current_state(conn)
    before = previous_snapshot(conn, run.run_id)
    created = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    conn.execute("DELETE FROM change_event WHERE run_id = ?", (run.run_id,))     # idempotent per run
    report = ChangeReport(run.run_id, baseline=before is None, projects=len(after))
    for ch in diff_states(before, after):
        mention_id = _evidence(conn, ch)
        ev = ChangeEvent(event_id=make_event_id(run.run_id, ch.project_id, ch.event_type), run_id=run.run_id,
                         project_id=ch.project_id, event_type=ch.event_type, old=ch.old, new=ch.new,
                         mention_id=mention_id, created_at=created)
        db._upsert(conn, ev)
        report.events.append(ev)
    save_snapshot(conn, run.run_id, after)
    run.counts = {**run.counts, "projects": len(after), "events": len(report.events), "baseline": int(report.baseline),
                  **{f"events_{k}": v for k, v in report.by_type.items()}}
    db.upsert_run(conn, run)
    return report


def events_for_runs(conn: sqlite3.Connection, run_ids: list[str]) -> list[ChangeEvent]:
    if not run_ids:
        return []
    marks = ",".join("?" * len(run_ids))
    rows = conn.execute(f"SELECT * FROM change_event WHERE run_id IN ({marks}) ORDER BY created_at, event_id", run_ids)
    return [db._from_row(ChangeEvent, r) for r in rows]
