"""M7: contract tests for the JSON listing adapters on small hand-built fixtures (no network).

The fixtures use the field names documented in RESEARCH.md §1.2; they are not recordings. If a live
endpoint changes, re-record, and these tests pinpoint the break.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from radar.adapters import joondalup_htmlresult as jh
from radar.adapters import kentico_ksearch as ks
from radar.adapters import list_council
from radar.settings import load_councils

FIX = Path(__file__).parent / "fixtures"
COUNCILS = load_councils().councils


def load(name):
    return json.loads((FIX / name).read_text())


def cfg(council, typ="kentico_ksearch"):
    return next(a.raw for a in COUNCILS[council].adapters if a.type == typ)


# ---- request builders (shapes copied from RESEARCH.md) ----------------------------------------------
def test_stirling_request_matches_research():
    url, headers, body = ks.build_request(cfg("stirling"))
    assert url == "https://www.stirling.wa.gov.au/ksearchmna/ksearchresult"
    assert body == {"take": 12, "skip": 0, "page": 1, "pageSize": 12}
    param = json.loads(headers["SearchParam"])
    assert {p["key"]: p["values"] for p in param} == {
        "classname": ["AWPT.MNA"], "searchindexes": ["MNA"],
        "nodealiaspath": ["/City-and-Council/About-Council/Council-and-committees/Minutes-and-agendas"]}
    assert json.loads(headers["SearchFilter"]) == [
        {"key": "Sort", "values": ["ArticleReleaseDate DESC"], "or": "true", "operator": ""}]
    assert " " not in headers["SearchFilter"].replace("ArticleReleaseDate DESC", "")   # compact JSON as documented


@pytest.mark.parametrize("council,endpoint", [
    ("melville", "https://www.melvillecity.com.au/ksearch/mnaresult"),
    ("rockingham", "https://rockingham.wa.gov.au/ksearch/mnaresult")])
def test_mna_request(council, endpoint):
    url, headers, body = ks.build_request(cfg(council))
    assert url == endpoint and "SearchFilter" not in headers
    param = json.loads(headers["SearchParam"])
    assert [(p["key"], p["values"]) for p in param] == [("classname", ["AWPT.MNA"]), ("searchindexes", ["MNA"])]
    assert isinstance(body, dict)


def test_joondalup_request_body_matches_research():
    assert jh.build_request_body(1, 24) == {
        "SI": "CouncilMeeting", "OB": "", "Q": "", "PS": 24, "FS": None, "PG": 1,
        "PR": [{"key": "widgetclassname", "values": [{"value": "AWCouncilMeetingSmartSearchListing"}]},
               {"key": "widgettemplatename", "values": [{"value": "AWCouncilMeetingSmartSearchListing"}]},
               {"key": "classname", "values": [{"value": "AWPT.CouncilMeeting"}]},
               {"key": "nodealiaspath", "values": [{"value": "/"}]}],
        "IncludeFirst": False}
    # every PR value is an object, never a plain string (plain strings gave HTTP 400)
    assert all(isinstance(v, dict) for p in jh.build_request_body()["PR"] for v in p["values"])


# ---- response contracts ----------------------------------------------------------------------------
def test_stirling_contract_and_mapping():
    payload = load("ksearch_stirling.json")
    assert "Data" in payload and "Total" in payload
    for rec in payload["Data"]:
        assert {"ArticleTitle", "ArticleReleaseDate", "Agenda", "MinutesFile", "Attachments"} <= rec.keys()
    refs = ks.parse_listing("stirling", payload, cfg("stirling"))
    assert sorted((r.meeting_date, r.doc_type) for r in refs) == [
        ("2026-11-03", "agenda"), ("2026-11-10", "agenda"), ("2026-11-10", "attachments"),
        ("2026-11-17", "agenda"), ("2026-11-17", "minutes")]
    assert refs[0].meeting_date == "2026-11-17"                      # newest first
    mins = next(r for r in refs if r.doc_type == "minutes")
    assert mins.url == "https://www.stirling.wa.gov.au/getmedia/bc87f6d5-383c-46c3-b4c6-9ece60b0be18/Council-Minutes-17-November-2026.pdf"
    assert mins.meeting_type == "Council Meeting 17 November 2026"
    assert len(refs) == 5                      # empty MinutesFile and null NoticeOfMotion are skipped
    assert all(r.url.startswith("https://www.stirling.wa.gov.au/") for r in refs)


def test_melville_contract_and_mapping():
    payload = load("ksearch_melville.json")
    for rec in payload["Data"]:
        assert {"MNAFirstFileName", "MNAFirstFileUrl", "MNAMeetingType", "MNAFirstFileSize", "PageUrl"} <= rec.keys()
    refs = ks.parse_listing("melville", payload, cfg("melville"))
    assert [r.meeting_date for r in refs] == ["2026-12-08", "2026-11-17", "2026-11-03"]
    dec = refs[0]
    assert dec.doc_type == "agenda" and dec.size_bytes == int(51.5 * 1048576)
    assert dec.url.startswith("https://www.melvillecity.com.au/getContentAsset/4c1124ba")
    assert refs[1].doc_type == "minutes" and refs[2].size_bytes == 4200000
    assert dec.meeting_type == "Ordinary Meeting of Council"


def test_rockingham_contract_and_mapping():
    payload = load("ksearch_rockingham.json")
    for rec in payload["Data"]:
        assert {"MNATitle", "MNAFileUrl", "MNADocumentType", "MNAMeetingType", "MNAFileSizeByte",
                "MNAPublicationDate"} <= rec.keys()
    refs = ks.parse_listing("rockingham", payload, cfg("rockingham"))
    types = {r.title: r.doc_type for r in refs}
    assert types["2026-11-(Nov)-Agenda-Ordinary-Council"] == "agenda"
    assert types["2026-10-(Oct)-Minutes-Ordinary-Council"] == "minutes"
    assert types["2026-11-(Nov)-Report-Attachments-Planning-and-Asset-Services"] == "attachments"
    assert types["2026-11-(Nov)-Information-Bulletin"] == "other"
    top = refs[0]
    assert top.meeting_date == "2026-11-19" and top.size_bytes == 5138022
    assert top.url.startswith("https://rockingham.wa.gov.au/getContentAsset/bf814a1d")


def test_missing_list_key_is_a_loud_contract_failure():
    with pytest.raises(KeyError, match="contract changed"):
        ks.parse_listing("stirling", {"Items": []}, cfg("stirling"))


def test_joondalup_parse_and_count():
    payload = load("joondalup_htmlresult_page.json")
    assert jh.record_count(payload) == 50
    assert jh.record_count({"htmlResult": '<input id="recordCountValue" value="840">'}) == 840
    assert jh.record_count({"htmlResult": "x"}) is None
    refs = jh.parse_html_result("joondalup", payload["htmlResult"], "https://www.joondalup.wa.gov.au/search/htmlresult")
    assert len(refs) == 4
    by = {r.meeting_date: r for r in refs if r.meeting_date}
    assert by["2026-10-27"].doc_type in ("agenda", "minutes")
    agenda = next(r for r in refs if r.doc_type == "agenda" and r.meeting_date == "2026-10-27")
    assert agenda.url == ("https://api.joondalup.wa.gov.au/files/councilmeetings/2026/"
                          "27%20Oct%202026%20-%20Council%20Agenda%20Inc%20Attachments.pdf?v=1")
    assert any(r.doc_type == "minutes" and r.meeting_date == "2026-10-27" for r in refs)
    brief = next(r for r in refs if r.meeting_type == "Briefing Session")
    assert brief.meeting_date == "2026-09-08"                       # "Septembe" typo tolerated
    old = next(r for r in refs if r.meeting_date == "2018-12-18")
    assert old.doc_type == "minutes" and old.url.startswith("https://www.joondalup.wa.gov.au/files/")


def test_joondalup_last_pages():
    assert jh.last_pages(840, 24) == [35, 34]
    assert jh.last_pages(841, 24) == [36, 35]
    assert jh.last_pages(10, 24) == [1]


# ---- list_council with a fake fetcher ------------------------------------------------------------------
class FakeFetcher:
    def __init__(self, tmp_path, responder):
        self.tmp, self.responder, self.calls = tmp_path, responder, []

    def fetch(self, url, method="GET", json_body=None, headers=None, **kw):
        self.calls.append(SimpleNamespace(url=url, method=method, body=json_body, headers=headers or {}))
        payload = self.responder(url, json_body)
        p = self.tmp / f"r{len(self.calls)}.json"
        p.write_text(json.dumps(payload))
        return SimpleNamespace(ok=True, path=p, read_bytes=p.read_bytes)


def test_list_council_stirling_posts_documented_request(tmp_path):
    f = FakeFetcher(tmp_path, lambda url, body: load("ksearch_stirling.json"))
    refs = list_council(COUNCILS["stirling"], f)
    (call,) = f.calls
    assert call.method == "POST" and call.body == {"take": 12, "skip": 0, "page": 1, "pageSize": 12}
    assert set(call.headers) == {"SearchParam", "SearchFilter"}
    assert len(refs) == 5 and all(r.extractor == "agenda_items" for r in refs)


def test_list_council_joondalup_reads_last_pages(tmp_path):
    page = load("joondalup_htmlresult_page.json")

    def responder(url, body):
        assert url == "https://www.joondalup.wa.gov.au/search/htmlresult" and body["SI"] == "CouncilMeeting"
        return {**page, "recordCountValue": 840}

    f = FakeFetcher(tmp_path, responder)
    council = COUNCILS["joondalup"]
    adapter = next(a for a in council.adapters if a.type == "joondalup_htmlresult")
    refs = jh.list_docs(council, adapter.raw, f)
    assert [c.body["PG"] for c in f.calls] == [1, 35, 34]            # probe, then the last two pages
    assert all(c.body["PS"] == 24 for c in f.calls)
    assert len(refs) == 4                                            # same fixture page twice -> de-duplicated

    f = FakeFetcher(tmp_path, lambda url, body: {**page, "recordCountValue": 10})
    jh.list_docs(council, adapter.raw, f)
    assert [c.body["PG"] for c in f.calls] == [1]                    # fits on one page: no extra requests


def test_listing_http_error_is_an_error_not_an_empty_listing(monkeypatch):
    """Melville/Rockingham ksearch returned HTTP 500 live (2026-11-22): that must be an adapter error."""

    from radar.adapters import list_adapter, sitemap
    from radar.adapters.base import ListingError
    from radar.fetch import FetchResult
    from radar.settings import Adapter

    class Fake:
        def fetch(self, url, **kw):
            return FetchResult(url, "http_error", status=500)

    council = SimpleNamespace(key="melville")
    cfg = {"endpoint": "https://www.melvillecity.com.au/ksearch/mnaresult", "body": {}, "search_param": [],
           "extractor": "agenda_items"}
    with pytest.raises(ListingError, match="HTTP 500"):
        ks.list_docs(council, cfg, Fake())
    monkeypatch.setattr(sitemap, "list_docs", lambda *a, **k: [])
    refs, errors, notes = list_adapter(council, Adapter("kentico_ksearch", {**cfg, "fallback": {"sitemap_url": "u"}}), Fake())
    assert refs == [] and notes == [] and "HTTP 500" in errors[0] and "sitemap fallback" in errors[0]
