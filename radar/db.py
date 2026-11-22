"""SQLite store (PLAN.md §2.4, §4.1). Stdlib ``sqlite3`` only.

Migrations are ``CREATE TABLE IF NOT EXISTS`` plus a ``schema_version`` row. Upserts are keyed on
the primary key, so re-running a stage is idempotent.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import fields
from pathlib import Path
from typing import Any, TypeVar

from radar import models as m
from radar.settings import SQLITE_PATH

SCHEMA_VERSION = 3   # 2: possible_duplicate (M12); 3: project_snapshot (M13)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS source_document (
  doc_id TEXT PRIMARY KEY, doc_key TEXT NOT NULL, version_no INTEGER NOT NULL DEFAULT 1,
  council_id TEXT NOT NULL, doc_type TEXT, title TEXT, meeting_type TEXT, meeting_date TEXT,
  financial_year TEXT, source_url TEXT NOT NULL, listing_url TEXT, retrieved_at TEXT,
  last_checked_at TEXT, http_etag TEXT, http_last_modified TEXT, content_type TEXT, bytes INTEGER,
  page_count INTEGER, text_sha256 TEXT, version_hint TEXT, extractor_version TEXT,
  extract_status TEXT, superseded_by TEXT
);
CREATE INDEX IF NOT EXISTS ix_source_document_key ON source_document(doc_key);

CREATE TABLE IF NOT EXISTS fetch_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, method TEXT NOT NULL, url TEXT NOT NULL,
  host TEXT NOT NULL, status INTEGER, bytes INTEGER, etag TEXT, last_modified TEXT, elapsed_ms INTEGER,
  robots_decision TEXT, cache_hit INTEGER NOT NULL DEFAULT 0, note TEXT
);

CREATE TABLE IF NOT EXISTS project_mention (
  mention_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL REFERENCES source_document(doc_id),
  page INTEGER NOT NULL, bbox TEXT, char_start INTEGER, char_end INTEGER,
  council_id TEXT NOT NULL, council_project_code TEXT, cost_code TEXT, tender_no TEXT,
  project_name_raw TEXT NOT NULL, location_raw TEXT, road_name TEXT, from_road TEXT, to_road TEXT,
  direction TEXT, suburb TEXT, ward TEXT, slk_from REAL, slk_to REAL,
  trade_labels TEXT NOT NULL DEFAULT '[]', value_amount REAL, value_basis TEXT,
  value_gst TEXT NOT NULL DEFAULT 'unknown', funding_refs TEXT NOT NULL DEFAULT '[]', funding_split TEXT,
  stage TEXT, date_stated TEXT, date_start TEXT, date_end TEXT, financial_year TEXT,
  resolution_ref TEXT, supplier_name TEXT, quote TEXT NOT NULL DEFAULT '',
  confidence REAL NOT NULL DEFAULT 0.5, uncertainty_flags TEXT NOT NULL DEFAULT '[]',
  extractor TEXT, extractor_version TEXT, extracted_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_mention_doc ON project_mention(doc_id);
CREATE INDEX IF NOT EXISTS ix_mention_council ON project_mention(council_id);

CREATE TABLE IF NOT EXISTS project (
  project_id TEXT PRIMARY KEY, council_id TEXT NOT NULL, dedupe_key TEXT NOT NULL, dedupe_key_type TEXT NOT NULL,
  canonical_name TEXT, road_name TEXT, from_road TEXT, to_road TEXT, suburb TEXT,
  trade_codes TEXT NOT NULL DEFAULT '[]', latest_value REAL, latest_value_basis TEXT, latest_stage TEXT,
  financial_year TEXT, first_seen TEXT, last_seen TEXT, confidence REAL NOT NULL DEFAULT 0.5,
  flags TEXT NOT NULL DEFAULT '[]', review_needed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS project_mention_link (
  mention_id TEXT PRIMARY KEY REFERENCES project_mention(mention_id),
  project_id TEXT NOT NULL REFERENCES project(project_id),
  link_method TEXT NOT NULL, link_score REAL NOT NULL DEFAULT 1.0
);
CREATE INDEX IF NOT EXISTS ix_link_project ON project_mention_link(project_id);

-- M12: pairs of projects that may be the same (fuzzy name match or cross-key conflict); never auto-merged.
CREATE TABLE IF NOT EXISTS possible_duplicate (
  project_a TEXT NOT NULL, project_b TEXT NOT NULL, method TEXT NOT NULL, score REAL NOT NULL DEFAULT 0,
  mention_id TEXT, PRIMARY KEY (project_a, project_b)
);

CREATE TABLE IF NOT EXISTS change_event (
  event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, project_id TEXT NOT NULL, event_type TEXT NOT NULL,
  old TEXT, new TEXT, mention_id TEXT, created_at TEXT
);

-- M13: per-run snapshot of each project's derived state; the next run diffs against the latest one.
CREATE TABLE IF NOT EXISTS project_snapshot (
  run_id TEXT NOT NULL, project_id TEXT NOT NULL, state TEXT NOT NULL, PRIMARY KEY (run_id, project_id)
);

CREATE TABLE IF NOT EXISTS run (
  run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, iso_week TEXT,
  councils TEXT NOT NULL DEFAULT '[]', counts TEXT NOT NULL DEFAULT '{}', errors TEXT NOT NULL DEFAULT '[]'
);
"""

_TABLES = {
    m.SourceDocument: "source_document",
    m.ProjectMention: "project_mention",
    m.FetchLogEntry: "fetch_log",
    m.Project: "project",
    m.ProjectMentionLink: "project_mention_link",
    m.ChangeEvent: "change_event",
    m.Run: "run",
}
_BOOL_FIELDS = {"cache_hit", "review_needed"}

T = TypeVar("T")


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open (creating if needed) the database and apply migrations. ``":memory:"`` is allowed."""
    target = str(path if path is not None else SQLITE_PATH)
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    conn.executescript(_SCHEMA)
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    current = row["v"] or 0
    if current < SCHEMA_VERSION:
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()
    return SCHEMA_VERSION


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0


def _to_row(obj: Any) -> dict[str, Any]:
    json_fields = set(type(obj).JSON_FIELDS)
    row: dict[str, Any] = {}
    for f in fields(obj):
        value = getattr(obj, f.name)
        if f.name in json_fields and value is not None:
            value = json.dumps(value, sort_keys=True, ensure_ascii=False)
        elif f.name in _BOOL_FIELDS:
            value = int(bool(value))
        row[f.name] = value
    return row


def _from_row(cls: type[T], row: sqlite3.Row) -> T:
    json_fields = set(cls.JSON_FIELDS)  # type: ignore[attr-defined]
    kwargs: dict[str, Any] = {}
    for f in fields(cls):  # type: ignore[arg-type]
        value = row[f.name]
        if f.name in json_fields and value is not None:
            value = json.loads(value)
        elif f.name in _BOOL_FIELDS:
            value = bool(value)
        kwargs[f.name] = value
    return cls(**kwargs)


def _upsert(conn: sqlite3.Connection, obj: Any) -> None:
    cls = type(obj)
    table = _TABLES[cls]
    row = _to_row(obj)
    pk = cls.PK
    if pk == "id" and row.get("id") is None:
        del row["id"]
        cols = list(row)
        conn.execute(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [row[c] for c in cols],
        )
        return
    cols = list(row)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != pk)
    conn.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
        f"ON CONFLICT({pk}) DO UPDATE SET {updates}",
        [row[c] for c in cols],
    )


def _get(conn: sqlite3.Connection, cls: type[T], key: Any) -> T | None:
    row = conn.execute(f"SELECT * FROM {_TABLES[cls]} WHERE {cls.PK} = ?", (key,)).fetchone()  # type: ignore[attr-defined]
    return _from_row(cls, row) if row else None


# --- source_document -------------------------------------------------------------------------
def upsert_source_document(conn: sqlite3.Connection, doc: m.SourceDocument) -> None:
    _upsert(conn, doc)
    conn.commit()


def get_source_document(conn: sqlite3.Connection, doc_id: str) -> m.SourceDocument | None:
    return _get(conn, m.SourceDocument, doc_id)


def latest_document_for_key(conn: sqlite3.Connection, doc_key: str) -> m.SourceDocument | None:
    """Newest version (highest ``version_no``) for a canonical URL, used to seed conditional GETs."""
    row = conn.execute(
        "SELECT * FROM source_document WHERE doc_key = ? ORDER BY version_no DESC LIMIT 1", (doc_key,)
    ).fetchone()
    return _from_row(m.SourceDocument, row) if row else None


# --- project_mention -------------------------------------------------------------------------
def upsert_mention(conn: sqlite3.Connection, mention: m.ProjectMention) -> None:
    _upsert(conn, mention)
    conn.commit()


def upsert_mentions(conn: sqlite3.Connection, mentions: list[m.ProjectMention]) -> int:
    for mention in mentions:
        _upsert(conn, mention)
    conn.commit()
    return len(mentions)


def get_mention(conn: sqlite3.Connection, mention_id: str) -> m.ProjectMention | None:
    return _get(conn, m.ProjectMention, mention_id)


def mentions_for_document(conn: sqlite3.Connection, doc_id: str) -> list[m.ProjectMention]:
    rows = conn.execute(
        "SELECT * FROM project_mention WHERE doc_id = ? ORDER BY page, char_start, mention_id", (doc_id,)
    ).fetchall()
    return [_from_row(m.ProjectMention, r) for r in rows]


# --- fetch_log -------------------------------------------------------------------------------
def insert_fetch_log(conn: sqlite3.Connection, entry: m.FetchLogEntry) -> None:
    _upsert(conn, entry)
    conn.commit()


def fetch_log_rows(conn: sqlite3.Connection, limit: int = 100) -> list[m.FetchLogEntry]:
    rows = conn.execute("SELECT * FROM fetch_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [_from_row(m.FetchLogEntry, r) for r in rows]


# --- project / link / event / run (used by later milestones) -----------------------------------
def upsert_project(conn: sqlite3.Connection, project: m.Project) -> None:
    _upsert(conn, project)
    conn.commit()


def get_project(conn: sqlite3.Connection, project_id: str) -> m.Project | None:
    return _get(conn, m.Project, project_id)


def upsert_link(conn: sqlite3.Connection, link: m.ProjectMentionLink) -> None:
    _upsert(conn, link)
    conn.commit()


def get_link(conn: sqlite3.Connection, mention_id: str) -> m.ProjectMentionLink | None:
    return _get(conn, m.ProjectMentionLink, mention_id)


def all_links(conn: sqlite3.Connection) -> list[m.ProjectMentionLink]:
    rows = conn.execute("SELECT * FROM project_mention_link ORDER BY project_id, mention_id").fetchall()
    return [_from_row(m.ProjectMentionLink, r) for r in rows]


def all_projects(conn: sqlite3.Connection, council_id: str | None = None) -> list[m.Project]:
    sql, args = "SELECT * FROM project", ()
    if council_id:
        sql, args = sql + " WHERE council_id = ?", (council_id,)
    return [_from_row(m.Project, r) for r in conn.execute(sql + " ORDER BY project_id", args).fetchall()]


def all_mentions(conn: sqlite3.Connection, council_id: str | None = None) -> list[m.ProjectMention]:
    sql, args = "SELECT * FROM project_mention", ()
    if council_id:
        sql, args = sql + " WHERE council_id = ?", (council_id,)
    return [_from_row(m.ProjectMention, r) for r in conn.execute(sql + " ORDER BY mention_id", args).fetchall()]


def all_source_documents(conn: sqlite3.Connection) -> list[m.SourceDocument]:
    return [_from_row(m.SourceDocument, r) for r in conn.execute("SELECT * FROM source_document").fetchall()]


def upsert_possible_duplicate(conn: sqlite3.Connection, a: str, b: str, method: str, score: float,
                              mention_id: str | None = None) -> None:
    """Record an unordered project pair once (the first evidence is kept)."""
    a, b = sorted((a, b))
    conn.execute("INSERT OR IGNORE INTO possible_duplicate (project_a, project_b, method, score, mention_id) "
                 "VALUES (?, ?, ?, ?, ?)", (a, b, method, score, mention_id))


def possible_duplicates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM possible_duplicate ORDER BY project_a, project_b").fetchall()


def upsert_change_event(conn: sqlite3.Connection, event: m.ChangeEvent) -> None:
    _upsert(conn, event)
    conn.commit()


def upsert_run(conn: sqlite3.Connection, run: m.Run) -> None:
    _upsert(conn, run)
    conn.commit()


def get_run(conn: sqlite3.Connection, run_id: str) -> m.Run | None:
    return _get(conn, m.Run, run_id)


def count(conn: sqlite3.Connection, cls: type) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {_TABLES[cls]}").fetchone()[0]
