"""Dedupe / linker (PLAN.md §5.2-§5.3, RESEARCH §5.4): assign mentions to projects with the key ladder.

Keys, strongest first (all scoped to one council):

1. ``code:``         council project code (Cockburn segment suffix included: ``4C.18#elderberry-drive|...``)
2. ``segment:``      normalised road | from | to. Work type is *not* in the key; instead a mention only joins a
                     segment project whose trade set intersects its own (rule labels, implied labels ignored;
                     a mention or project whose only label is UNKNOWN is compatible with anything). This stops
                     a resurfacing and a drainage job on the same segment from merging.
3. ``intersection:`` sorted pair of normalised road names, parsed from ``location_raw`` or the project name
4. ``tender:``       tender number
   ``version:``      (extension) the same normalised name in another version of the same document
                     (same ``doc_key``), so a re-published document does not create look-alike duplicates
5. ``fuzzy``         rapidfuzz ``token_set_ratio`` >= 90 on the normalised name, same council, same or adjacent
                     FY. **Never merges**: the mention gets its own project, both projects get
                     ``possible_duplicate`` + ``review_needed`` and the pair goes into ``possible_duplicate``.
6. otherwise a new ``singleton`` project.

A mention is linked to the project found by its strongest matching key, and *all* its keys then point at
that project, so later mentions can arrive through any of them. When different keys of one mention point at
different projects, the strongest wins and the pair is recorded as a possible duplicate (no merge in v1).

``link()`` is incremental and idempotent: only unlinked mentions are linked, in a deterministic order, and
every touched project's state is re-derived from its mentions (§5.3). ``rebuild=True`` clears links,
projects and duplicate pairs first and relinks everything.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime

from rapidfuzz import fuzz

from radar import db
from radar.models import Project, ProjectMention, ProjectMentionLink, SourceDocument, make_project_id
from radar.normalise import normalise_name, normalise_road, parse_intersection

KEY_RANK = ("code", "segment", "intersection", "tender", "version")
FUZZY_THRESHOLD = 90
CROSS_CHECK_COUNCIL = "mainroads"             # council_id of cross-check *documents* (not a configured council)
CROSS_CHECK_EXTRACTORS = ("blackspot",)       # cross-check mentions never create or shape projects (PLAN M17)
STAGE_ORDER = ("proposed_budget", "adopted_budget", "budget_review", "tender_advertised", "tender_awarded",
               "in_progress", "completed")
OVERRIDE_STAGES = ("deferred", "deleted")
BASIS_PREFERENCE = {"contract_award": 3, "budget_total": 2, "current_year": 1}
_NO_TRADE = {"UNKNOWN"}


# ---- keys --------------------------------------------------------------------------------------
def _fy_start(fy: str | None) -> int | None:
    m = re.match(r"(\d{4})", fy or "")
    return int(m.group(1)) if m else None


def mention_keys(m: ProjectMention, doc_key: str | None = None) -> dict[str, str]:
    """All dedupe keys of a mention, by type (absent types omitted)."""
    keys: dict[str, str] = {}
    if m.council_project_code:
        keys["code"] = "code:" + re.sub(r"\s+", "", m.council_project_code).upper()
    road, frm, to = (normalise_road(x) for x in (m.road_name, m.from_road, m.to_road))
    if road and frm and to:
        keys["segment"] = f"segment:{road}|{frm}|{to}"
    pair = parse_intersection(m.location_raw) or parse_intersection(m.project_name_raw)
    if pair:
        keys["intersection"] = f"intersection:{pair[0]}|{pair[1]}"
    if m.tender_no:
        keys["tender"] = "tender:" + re.sub(r"\s+", "", m.tender_no).upper()
    name = normalise_name(m.project_name_raw)
    if doc_key and name:
        keys["version"] = f"version:{doc_key}|{name}"
    return keys


def trade_set(labels: list[dict]) -> set[str]:
    """Rule labels only (implied labels would make every road job 'intersect' drainage)."""
    return {lab["code"] for lab in labels or [] if lab.get("method") != "rule_implied"}


def trades_compatible(a: set[str], b: set[str]) -> bool:
    a, b = a - _NO_TRADE, b - _NO_TRADE
    return not a or not b or bool(a & b)


def document_date(d: SourceDocument | None) -> str:
    """ISO date a document speaks for: meeting date, else HTTP Last-Modified, else retrieval date (PLAN §5.3).

    Last-Modified comes before retrieved_at so an annual document fetched today does not look newer than a
    meeting held last week."""
    if d is None:
        return ""
    if d.meeting_date:
        return d.meeting_date
    if d.http_last_modified:
        try:
            return parsedate_to_datetime(d.http_last_modified).date().isoformat()
        except (TypeError, ValueError, IndexError):
            pass
    return (d.retrieved_at or "")[:10]


# ---- linker state ------------------------------------------------------------------------------
@dataclass
class _Proj:
    project_id: str
    council_id: str
    dedupe_key: str
    dedupe_key_type: str
    trades: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)
    fys: set[int] = field(default_factory=set)
    strong: dict[str, set[str]] = field(default_factory=dict)      # key type -> values (code/segment/tender)


@dataclass
class LinkReport:
    linked: Counter = field(default_factory=Counter)                # link_method -> count (this run)
    projects_touched: int = 0
    projects_total: int = 0
    duplicates_new: int = 0

    def summary(self) -> str:
        methods = ", ".join(f"{k}={v}" for k, v in sorted(self.linked.items())) or "none"
        return (f"linked {sum(self.linked.values())} mention(s) ({methods}); projects updated {self.projects_touched}, "
                f"total {self.projects_total}; new possible duplicates {self.duplicates_new}")


class Linker:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.docs: dict[str, SourceDocument] = {d.doc_id: d for d in db.all_source_documents(conn)}
        self.projects: dict[str, _Proj] = {}
        self.index: dict[tuple[str, str], list[str]] = {}           # (council, key) -> project ids
        self.touched: set[str] = set()
        self.report = LinkReport()

    # -- helpers --
    def doc_key(self, m: ProjectMention) -> str | None:
        d = self.docs.get(m.doc_id)
        return d.doc_key if d else None

    def doc_date(self, m: ProjectMention) -> str:
        return document_date(self.docs.get(m.doc_id))

    def order_key(self, m: ProjectMention) -> tuple:
        d = self.docs.get(m.doc_id)
        return (self.doc_date(m), d.retrieved_at or "" if d else "", m.doc_id, m.page, m.char_start or 0,
                (m.bbox or [0, 0])[1], m.mention_id)

    def _register(self, proj: _Proj, m: ProjectMention, keys: dict[str, str]) -> list[str]:
        """Point every key of ``m`` at ``proj``; return projects that already held one of those keys."""
        others: list[str] = []
        for ktype, key in keys.items():
            ids = self.index.setdefault((proj.council_id, key), [])
            if proj.project_id not in ids:
                if ktype not in ("segment", "version"):     # shared segments / names are not conflicts
                    others.extend(i for i in ids if i != proj.project_id)
                ids.append(proj.project_id)
            if ktype in ("code", "segment", "tender"):
                proj.strong.setdefault(ktype, set()).add(key)
        proj.trades |= trade_set(m.trade_labels)
        name = normalise_name(m.project_name_raw)
        if name:
            proj.names.add(name)
        fy = _fy_start(m.financial_year)
        if fy:
            proj.fys.add(fy)
        return others

    def _candidate(self, council: str, ktype: str, key: str, trades: set[str], strong: dict[str, set[str]]) -> str | None:
        ids = self.index.get((council, key), [])
        for pid in sorted(ids, key=lambda i: (self.projects[i].dedupe_key != key, i)):
            proj = self.projects[pid]
            if ktype == "segment" and not trades_compatible(trades, proj.trades):
                continue
            if any(t in strong and t in proj.strong and not (strong[t] & proj.strong[t]) for t in ("code", "tender")):
                continue          # a weaker key never joins a mention to a project with a different code / tender
            return pid
        return None

    def _new_project(self, m: ProjectMention, keys: dict[str, str], trades: set[str]) -> _Proj:
        ktype = next((k for k in KEY_RANK if k in keys), None)
        key = keys[ktype] if ktype else f"singleton:{m.mention_id}"
        if ktype == "segment" and make_project_id(m.council_id, key) in self.projects:
            # the same segment is already a project with an incompatible trade set: a separate job
            base = f"{key}#{'+'.join(sorted(trades - _NO_TRADE)) or 'other'}"
            key, n = base, 1
            while make_project_id(m.council_id, key) in self.projects:
                n += 1
                key = f"{base}#{n}"
        pid = make_project_id(m.council_id, key)
        proj = self.projects.get(pid) or _Proj(pid, m.council_id, key, ktype or "singleton")
        self.projects[pid] = proj
        if db.get_project(self.conn, pid) is None:      # minimal row first (FK); state is derived later
            db._upsert(self.conn, Project(pid, m.council_id, key, proj.dedupe_key_type))
        return proj

    def _conflicting(self, a: _Proj, b: _Proj) -> bool:
        """Two projects that both carry a strong key of the same type with different values are distinct."""
        return any(t in a.strong and t in b.strong and not (a.strong[t] & b.strong[t]) for t in ("code", "tender", "segment"))

    def _fuzzy(self, m: ProjectMention, proj: _Proj) -> tuple[str, float] | None:
        name = normalise_name(m.project_name_raw)
        tokens = set(name.split())
        fy = _fy_start(m.financial_year)
        best: tuple[str, float] | None = None
        for other in self.projects.values():
            if other.project_id == proj.project_id or other.council_id != m.council_id:
                continue
            if fy and other.fys and min(abs(fy - y) for y in other.fys) > 1:
                continue
            if self._conflicting(proj, other):
                continue
            for oname in other.names:
                if len(tokens & set(oname.split())) < 2:
                    continue
                score = fuzz.token_set_ratio(name, oname)
                if score >= FUZZY_THRESHOLD and (best is None or score > best[1] or
                                                 (score == best[1] and other.project_id < best[0])):
                    best = (other.project_id, score)
        return best

    # -- loading existing links --
    def load(self) -> None:
        mentions = {m.mention_id: m for m in db.all_mentions(self.conn)}
        projects = {p.project_id: p for p in db.all_projects(self.conn)}
        for link in sorted(db.all_links(self.conn), key=lambda l: self.order_key(mentions[l.mention_id])
                           if l.mention_id in mentions else ()):
            m = mentions.get(link.mention_id)
            p = projects.get(link.project_id)
            if m is None or p is None or m.extractor in CROSS_CHECK_EXTRACTORS:
                continue
            proj = self.projects.setdefault(p.project_id, _Proj(p.project_id, p.council_id, p.dedupe_key, p.dedupe_key_type))
            self._register(proj, m, mention_keys(m, self.doc_key(m)))

    # -- linking --
    def link_mention(self, m: ProjectMention) -> ProjectMentionLink:
        keys = mention_keys(m, self.doc_key(m))
        trades = trade_set(m.trade_labels)
        strong = {t: {keys[t]} for t in ("code", "tender") if t in keys}
        matches: list[tuple[str, str]] = []                       # (key type, project id) in rank order
        for ktype in KEY_RANK:
            if ktype in keys:
                pid = self._candidate(m.council_id, ktype, keys[ktype], trades, strong)
                if pid:
                    matches.append((ktype, pid))
        if matches:
            method, pid = matches[0]
            proj = self.projects[pid]
            score = 1.0
        else:
            proj = self._new_project(m, keys, trades)
            method, score = "singleton", 1.0
        others = self._register(proj, m, keys)
        conflict = {p for _k, p in matches[1:] if p != proj.project_id} | {p for p in others if p != proj.project_id}
        for other in sorted(conflict):
            self._duplicate(proj.project_id, other, "cross_key", 1.0, m.mention_id)
        if method == "singleton":
            hit = self._fuzzy(m, proj)
            if hit:
                method, score = "fuzzy", round(hit[1] / 100, 3)
                self._duplicate(proj.project_id, hit[0], "fuzzy", score, m.mention_id)
        self.touched.add(proj.project_id)
        self.report.linked[method] += 1
        link = ProjectMentionLink(proj.project_id, m.mention_id, method, score)
        db._upsert(self.conn, link)
        return link

    def _duplicate(self, a: str, b: str, method: str, score: float, mention_id: str) -> None:
        if self._conflicting(self.projects[a], self.projects[b]):
            return                                  # different codes / tenders / segments: distinct by definition
        before = self.conn.total_changes
        db.upsert_possible_duplicate(self.conn, a, b, method, score, mention_id)
        if self.conn.total_changes > before:
            self.report.duplicates_new += 1
        self.touched.update((a, b))


# ---- project state (§5.3) ----------------------------------------------------------------------
def derive_state(project: Project, mentions: list[ProjectMention], docs: dict[str, SourceDocument],
                 duplicate: bool) -> Project:
    def date(m: ProjectMention) -> str:
        return document_date(docs.get(m.doc_id))

    def seen(m: ProjectMention) -> str:
        d = docs.get(m.doc_id)
        return (d.retrieved_at if d and d.retrieved_at else m.extracted_at) or ""

    # cross-check mentions (Black Spot) only add funding / sources: they never set name, value, stage or location
    mentions = [m for m in mentions if m.extractor not in CROSS_CHECK_EXTRACTORS] or mentions
    by_conf = sorted(mentions, key=lambda m: (-m.confidence, date(m), m.mention_id))
    p = project
    p.canonical_name = by_conf[0].project_name_raw if by_conf else p.canonical_name
    for attr in ("road_name", "from_road", "to_road", "suburb"):
        setattr(p, attr, next((getattr(m, attr) for m in by_conf if getattr(m, attr)), None))

    codes: list[str] = []
    for m in by_conf:
        for lab in m.trade_labels or []:
            if lab["code"] not in codes:
                codes.append(lab["code"])
    if len(codes) > 1 and "UNKNOWN" in codes:
        codes.remove("UNKNOWN")
    p.trade_codes = sorted(codes)

    valued = [m for m in mentions if m.value_amount is not None]
    if valued:
        best = max(valued, key=lambda m: (date(m), BASIS_PREFERENCE.get(m.value_basis or "", 0), seen(m), m.mention_id))
        p.latest_value, p.latest_value_basis = best.value_amount, best.value_basis
    else:
        p.latest_value = p.latest_value_basis = None

    staged = [m for m in mentions if m.stage in STAGE_ORDER]
    stage, stage_date = None, ""
    if staged:
        top = max(staged, key=lambda m: (STAGE_ORDER.index(m.stage), date(m)))
        stage, stage_date = top.stage, date(top)
    overrides = [m for m in mentions if m.stage in OVERRIDE_STAGES]
    if overrides:
        latest = max(overrides, key=lambda m: (date(m), m.stage == "deleted"))
        if stage is None or date(latest) >= stage_date:
            stage = latest.stage
    p.latest_stage = stage

    with_fy = [m for m in mentions if m.financial_year]
    p.financial_year = max(with_fy, key=lambda m: (date(m), m.financial_year)).financial_year if with_fy else None
    times = sorted(seen(m) for m in mentions if seen(m))
    p.first_seen, p.last_seen = (times[0], times[-1]) if times else (None, None)
    p.confidence = max((m.confidence for m in mentions), default=0.5)
    flags = sorted({f for m in mentions for f in (m.uncertainty_flags or [])} | ({"possible_duplicate"} if duplicate else set()))
    p.flags = flags
    p.review_needed = duplicate
    return p


def refresh_projects(conn: sqlite3.Connection, project_ids: set[str] | None = None) -> int:
    """Re-derive state for the given projects (all if None) from their linked mentions."""
    docs = {d.doc_id: d for d in db.all_source_documents(conn)}
    dup_ids = {r["project_a"] for r in db.possible_duplicates(conn)} | {r["project_b"] for r in db.possible_duplicates(conn)}
    by_project: dict[str, list[str]] = {}
    for link in db.all_links(conn):
        by_project.setdefault(link.project_id, []).append(link.mention_id)
    targets = project_ids if project_ids is not None else set(by_project)
    n = 0
    for pid in sorted(targets):
        project = db.get_project(conn, pid)
        mids = by_project.get(pid, [])
        if project is None or not mids:
            continue
        mentions = [x for x in (db.get_mention(conn, mid) for mid in mids) if x]
        db._upsert(conn, derive_state(project, mentions, docs, pid in dup_ids))
        n += 1
    conn.commit()
    return n


def clear(conn: sqlite3.Connection, council_id: str | None = None) -> None:
    if council_id:
        pids = [r[0] for r in conn.execute("SELECT project_id FROM project WHERE council_id = ?", (council_id,))]
        conn.executemany("DELETE FROM project_mention_link WHERE project_id = ?", [(p,) for p in pids])
        conn.executemany("DELETE FROM possible_duplicate WHERE project_a = ? OR project_b = ?", [(p, p) for p in pids])
        conn.executemany("DELETE FROM project WHERE project_id = ?", [(p,) for p in pids])
    else:
        conn.execute("DELETE FROM project_mention_link")
        conn.execute("DELETE FROM possible_duplicate")
        conn.execute("DELETE FROM project")
    conn.commit()


def link(conn: sqlite3.Connection, *, council_id: str | None = None, rebuild: bool = False) -> LinkReport:
    """Link every unlinked mention (optionally one council) and refresh the state of touched projects."""
    if rebuild:
        clear(conn, council_id)
    linker = Linker(conn)
    linker.load()
    linked = {r[0] for r in conn.execute("SELECT mention_id FROM project_mention_link")}
    todo = [m for m in db.all_mentions(conn, council_id)
            if m.mention_id not in linked and m.extractor not in CROSS_CHECK_EXTRACTORS]
    todo.sort(key=linker.order_key)
    for m in todo:
        linker.link_mention(m)
    conn.commit()
    linker.report.projects_touched = refresh_projects(conn, linker.touched)
    linker.report.projects_total = db.count(conn, Project)
    return linker.report
