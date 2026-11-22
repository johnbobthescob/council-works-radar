"""Black Spot cross-check (PLAN.md §3.2 licence caution, §4.3, M17).

``blackspot`` mentions (Main Roads WA list, target-council rows only) never become projects. After the normal
linking, ``tag`` attaches each one to a *council-sourced* project of the same council, so the project shows
``BlackSpot_State`` funding and the Main Roads document among its sources:

1. intersection: the order-independent road pair of the row equals the pair of a project's mentions / name;
2. road: the row's road equals the road of exactly one project (and, where both sides state an SLK range, the
   ranges overlap). Several candidates on the same road -> no tag (never guess).

The link uses ``link_method='blackspot_xref'``. Unmatched rows stay unlinked (and therefore are never shown).
``tag`` only processes unlinked mentions, so re-running it changes nothing.
"""

from __future__ import annotations

import sqlite3

from radar import db
from radar.dedupe import CROSS_CHECK_EXTRACTORS, mention_keys, refresh_projects
from radar.extract.blackspot import split_location
from radar.models import ProjectMention, ProjectMentionLink
from radar.normalise import normalise_road, parse_intersection

LINK_METHOD = "blackspot_xref"
FUNDING_REF = "BlackSpot_State"


def _pair(m: ProjectMention) -> tuple[str, str] | None:
    """Sorted normalised road pair of an intersection row/mention."""
    for text in (m.location_raw, m.project_name_raw):
        if not text:
            continue
        road, cross = split_location(text) if m.extractor in CROSS_CHECK_EXTRACTORS else (None, None)
        if cross:
            a, b = normalise_road(road), normalise_road(cross)
            if a and b and a != b:
                return tuple(sorted((a, b)))  # type: ignore[return-value]
        pair = parse_intersection(text)
        if pair:
            return pair
    return None


def _overlap(a: ProjectMention, b: ProjectMention) -> bool | None:
    if None in (a.slk_from, a.slk_to, b.slk_from, b.slk_to):
        return None
    return min(a.slk_to, b.slk_to) >= max(a.slk_from, b.slk_from)


def tag(conn: sqlite3.Connection) -> int:
    """Link unlinked Black Spot mentions to matching council projects; returns the number of new links."""
    all_m = db.all_mentions(conn)
    linked = {l.mention_id: l for l in db.all_links(conn)}
    bs = [m for m in all_m if m.extractor in CROSS_CHECK_EXTRACTORS and m.mention_id not in linked]
    if not bs:
        return 0
    by_id = {m.mention_id: m for m in all_m}
    pair_index: dict[tuple[str, str, str], set[str]] = {}
    road_index: dict[tuple[str, str], set[str]] = {}
    members: dict[str, list[ProjectMention]] = {}
    projects = {p.project_id: p for p in db.all_projects(conn)}
    for mid, link in linked.items():
        m = by_id.get(mid)
        p = projects.get(link.project_id)
        if m is None or p is None or m.extractor in CROSS_CHECK_EXTRACTORS:
            continue
        members.setdefault(p.project_id, []).append(m)
        pair = _pair(m)
        if not pair and "intersection" in mention_keys(m):
            pair = tuple(mention_keys(m)["intersection"].split(":", 1)[1].split("|"))
        if pair:
            pair_index.setdefault((p.council_id, *pair), set()).add(p.project_id)
        for road in (m.road_name, p.road_name):
            if road and not pair:
                road_index.setdefault((p.council_id, normalise_road(road)), set()).add(p.project_id)

    touched: set[str] = set()
    n = 0
    for m in sorted(bs, key=lambda x: x.mention_id):
        pair = _pair(m)
        choice: tuple[str, float] | None = None
        if pair:
            ids = pair_index.get((m.council_id, *pair), set())
            if ids:
                choice = (min(ids), 1.0)
        elif m.road_name:
            ids = set()
            for pid in road_index.get((m.council_id, normalise_road(m.road_name)), set()):
                verdicts = [_overlap(m, x) for x in members.get(pid, [])]
                if verdicts.count(True) or not verdicts.count(False):
                    ids.add(pid)
            if len(ids) == 1:                 # road-only evidence is weak: tag only an unambiguous project
                choice = (ids.pop(), 0.7)
        if choice:
            db._upsert(conn, ProjectMentionLink(choice[0], m.mention_id, LINK_METHOD, choice[1]))
            touched.add(choice[0])
            n += 1
    conn.commit()
    if touched:
        refresh_projects(conn, touched)
    return n
