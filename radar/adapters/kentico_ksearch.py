"""Listing adapter: kentico_ksearch (PLAN.md §3.1; Stirling, Melville, Rockingham).

``POST <endpoint>`` with a JSON body and two JSON-valued headers, ``SearchParam`` and (optionally)
``SearchFilter`` (R§1.2.3). Shapes are copied from RESEARCH.md; per-site differences live in
``config/councils.yaml``:

* Stirling: ``/ksearchmna/ksearchresult`` -> ``{"Data":[{ArticleTitle, ArticleReleaseDate, Agenda,
  MinutesFile, NoticeOfMotion, Attachments[]}], "Total": n}``.
* Melville / Rockingham: ``/ksearch/mnaresult`` -> flat ``MNA*`` fields, mapped by ``response_fields``.

Not documented in RESEARCH and therefore assumptions (flagged in the config / README): the
``or``/``operator`` keys inside ``SearchParam`` entries (R§ shows ``...``; we mirror the documented
``SearchFilter`` entry shape), the Melville/Rockingham body, and the list key (``Data`` everywhere).
"""

from __future__ import annotations

import json
from typing import Any

from radar.adapters.base import DocRef, absolute, doc_type_from_text, parse_size
from radar.dates import parse_date_flexible, parse_date_text

ADAPTER = "kentico_ksearch"
DEFAULT_BODY = {"take": 12, "skip": 0, "page": 1, "pageSize": 12}
URL_KEYS = ("Url", "URL", "FileUrl", "FileURL", "Path", "Link", "Href", "href")
NAME_KEYS = ("Name", "Title", "FileName", "Text")
MNA_DOC_TYPES = {"agenda": "agenda", "minutes": "minutes", "report attachments": "attachments",
                 "attachments": "attachments", "bulletins": "other"}


def _entries(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Entry shape per R§1.2.3 SearchFilter: {"key":..., "values":[...], "or":"true", "operator":""}.
    return [{"key": i["key"], "values": list(i["values"]), "or": "true", "operator": ""} for i in items]


def _dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def build_request(cfg: dict[str, Any]) -> tuple[str, dict[str, str], dict[str, Any]]:
    """(url, headers, json body) for one listing call."""
    headers: dict[str, str] = {}      # Content-Type: application/json is set by the fetcher's json= body
    if cfg.get("search_param"):
        headers["SearchParam"] = _dumps(_entries(cfg["search_param"]))
    if cfg.get("search_filter"):
        headers["SearchFilter"] = _dumps(_entries(cfg["search_filter"]))
    return cfg["endpoint"], headers, dict(cfg.get("body") or DEFAULT_BODY)


def _url_of(value: Any) -> str | None:
    """A URL from a string or object value (tolerant: the nested file objects are not fully documented)."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for k in URL_KEYS:
            if isinstance(value.get(k), str) and value[k].strip():
                return value[k].strip()
    return None


def _name_of(value: Any) -> str | None:
    if isinstance(value, dict):
        for k in NAME_KEYS:
            if isinstance(value.get(k), str) and value[k].strip():
                return value[k].strip()
    return None


def _file_items(value: Any) -> list[Any]:
    if value in (None, "", []):
        return []
    return value if isinstance(value, list) else [value]


def records(payload: Any, list_key: str = "Data") -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        data = payload.get(list_key)
        if data is None:
            raise KeyError(f"response has no {list_key!r} key (keys: {sorted(payload)}); endpoint contract changed?")
        return [r for r in data if isinstance(r, dict)]
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    raise TypeError("unexpected ksearch response")


def parse_nested(council_id: str, payload: Any, base_url: str, cfg: dict[str, Any]) -> list[DocRef]:
    """Stirling-style records: one meeting with Agenda / MinutesFile / Attachments."""
    fields = cfg.get("fields") or {"agenda": "Agenda", "minutes": "MinutesFile", "attachments": "Attachments"}
    refs: list[DocRef] = []
    for rec in records(payload, cfg.get("response_list_key", "Data")):
        title = rec.get("ArticleTitle")
        date = parse_date_flexible(rec.get("ArticleReleaseDate")) or parse_date_text(title)
        for logical, doc_type in (("agenda", "agenda"), ("minutes", "minutes"), ("attachments", "attachments")):
            for item in _file_items(rec.get(fields.get(logical, ""))):
                url = _url_of(item)
                if not url:
                    continue
                label = _name_of(item)
                refs.append(DocRef(
                    council_id=council_id, url=absolute(base_url, url), doc_type=doc_type,
                    title=f"{title} - {label}" if label and title else (title or label),
                    meeting_type=title, meeting_date=date, listing_url=base_url,
                    size_bytes=parse_size(item.get("Size") or item.get("FileSize")) if isinstance(item, dict) else None,
                    adapter=ADAPTER))
    return refs


def parse_flat(council_id: str, payload: Any, base_url: str, cfg: dict[str, Any]) -> list[DocRef]:
    """Melville / Rockingham records mapped by ``response_fields`` (logical name -> JSON key)."""
    f = cfg["response_fields"]
    refs: list[DocRef] = []
    for rec in records(payload, cfg.get("response_list_key", "Data")):
        get = lambda logical: rec.get(f[logical]) if logical in f else None  # noqa: E731
        url = _url_of(get("file_url"))
        if not url:
            continue
        title = get("title") or get("file_name")
        raw_type = (get("document_type") or "").strip().lower()
        doc_type = MNA_DOC_TYPES.get(raw_type) or doc_type_from_text(f"{title or ''} {url}")
        date = (parse_date_flexible(get("publication_date")) or parse_date_text(str(get("file_name") or ""))
                or parse_date_text(str(title or "")) or parse_date_text(str(get("page_url") or "")) or parse_date_text(url))
        refs.append(DocRef(
            council_id=council_id, url=absolute(base_url, url), doc_type=doc_type, title=title,
            meeting_type=get("meeting_type"), meeting_date=date, listing_url=absolute(base_url, get("page_url")) if get("page_url") else base_url,
            size_bytes=parse_size(get("file_size")), adapter=ADAPTER))
    return refs


def parse_listing(council_id: str, payload: Any, cfg: dict[str, Any]) -> list[DocRef]:
    base = cfg["endpoint"]
    refs = parse_nested(council_id, payload, base, cfg) if cfg.get("fields") else parse_flat(council_id, payload, base, cfg)
    return sorted(refs, key=lambda r: r.meeting_date or "", reverse=True)


def list_docs(council, cfg: dict[str, Any], fetcher) -> list[DocRef]:
    from radar.settings import LISTING_MAX_BYTES

    url, headers, body = build_request(cfg)
    res = fetcher.fetch(url, method="POST", json_body=body, headers=headers, max_bytes=LISTING_MAX_BYTES)
    if not res.ok or not res.path:
        return []
    return parse_listing(council.key, json.loads(res.read_bytes()), cfg)
