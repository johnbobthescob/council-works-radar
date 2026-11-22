"""M18: sitemap fallback adapter, fallback wiring, and extracted program totals (offline, loopback only)."""

import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from radar import adapters, changes, db, dedupe, digest, export
from radar.adapters import sitemap
from radar.fetch import Fetcher
from radar.models import ProjectMention, SourceDocument, make_mention_id
from radar.pipeline import CouncilResult, list_refs
from radar.settings import Adapter, Council, CouncilsConfig, Defaults

NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
BASE = "/our-city/our-council/agendas-and-minutes"
PAGES = [
    (f"{BASE}/agenda-ordinary-meeting-of-council-17-november-2026", "2026-11-10"),
    (f"{BASE}/minutes-ordinary-meeting-of-council-20-october-2026", "2026-10-30"),
    (f"{BASE}/agenda-special-meeting-of-council-1-september-2026", "2026-09-01"),
    ("/about-us", "2026-01-01"),
    (f"{BASE}", "2026-01-01"),
    ("/news/agenda-for-change", "2026-01-01"),
]
PATTERN = BASE + r"/(agenda|minutes)-[a-z-]*\d{1,2}-[a-z]+-20\d{2}"


def urlset(base="", ns=True):
    rows = "".join(f"<url><loc>{base}{p}</loc><lastmod>{m}</lastmod></url>" for p, m in PAGES)
    return f'<?xml version="1.0"?><urlset{f" xmlns=\"{NS}\"" if ns else ""}>{rows}</urlset>'.encode()


def index(base):
    return (f'<sitemapindex xmlns="{NS}">' + "".join(
        f"<sitemap><loc>{base}/sm{i}.xml</loc></sitemap>" for i in range(1, 6)) + "</sitemapindex>").encode()


def test_parse_urlset_with_and_without_namespace():
    for ns in (True, False):
        entries, children = sitemap.parse_sitemap(urlset("https://x.test", ns))
        assert children == [] and len(entries) == 6
        assert entries[0] == {"loc": f"https://x.test{PAGES[0][0]}", "lastmod": "2026-11-10"}
    assert sitemap.parse_sitemap(urlset("https://x.test").decode())[0][1]["loc"].endswith("20-october-2026")


def test_parse_sitemapindex():
    entries, children = sitemap.parse_sitemap(index("https://x.test"))
    assert entries == [] and children[0] == "https://x.test/sm1.xml" and len(children) == 5


@pytest.mark.parametrize("evil", [
    b'<?xml version="1.0"?><!DOCTYPE urlset [<!ENTITY a "b">]><urlset><url><loc>&a;</loc></url></urlset>',
    b'<!doctype urlset><urlset/>', b'<!ENTITY x "y"><urlset/>'])
def test_dtd_and_entities_rejected(evil):
    with pytest.raises(ValueError):
        sitemap.parse_sitemap(evil)


def test_invalid_xml_rejected():
    with pytest.raises(ValueError):
        sitemap.parse_sitemap(b"<urlset><url>")


def test_entry_to_ref_derives_metadata():
    ref = sitemap.entry_to_ref("melville", {"loc": f"https://x.test{PAGES[0][0]}"})
    assert (ref.doc_type, ref.meeting_date, ref.meeting_type, ref.fmt) == (
        "agenda", "2026-11-17", "Ordinary Council Meeting", "html")
    ref = sitemap.entry_to_ref("melville", {"loc": "https://x.test/minutes-of-council", "lastmod": "2026-05-04T01:00:00Z"})
    assert ref.doc_type == "minutes" and ref.meeting_date == "2026-05-04"


# ---- loopback server ----------------------------------------------------------------------------
MEETING_PAGE = (b'<html><body><a href="/getmedia/abc/Agenda-17-Nov-2026.pdf">Agenda (PDF)</a>'
                b'<a href="/about">About</a><a href="/files/att.pdf">Attachments</a></body></html>')


class Srv:
    def __init__(self, files):
        self.files, self.hits = files, []
        hits = self.hits
        files_ = files

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                hits.append(self.path)
                body = files_.get(self.path)
                code = 200 if body is not None else 404
                if self.path == "/robots.txt":
                    body, code = b"User-agent: *\nAllow: /\n", 200
                body = body if body is not None else b"nope"
                self.send_response(code)
                self.send_header("Content-Type", "text/html" if not self.path.endswith(".xml") else "application/xml")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_POST = do_GET

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"


@pytest.fixture
def server():
    made = []

    def make(files):
        s = Srv(files)
        made.append(s)
        return s
    yield make
    for s in made:
        s.httpd.shutdown()
        s.httpd.server_close()


def fetcher_for(tmp_path, base, cfg_adapters=()):
    host = base.split("//")[1]
    council = Council(key="melville", name="Melville", enabled=True, hosts=[host], blocked_hosts=[], terms_url=None,
                      adapters=[Adapter(a["type"], a) for a in cfg_adapters])
    config = CouncilsConfig(defaults=Defaults(min_delay_s=0.0), global_blocklist=[], councils={"melville": council})
    conn = db.connect(tmp_path / "r.sqlite")
    return council, Fetcher(config=config, conn=conn, cache_dir=tmp_path / "cache", log_path=tmp_path / "f.jsonl",
                            pause_path=tmp_path / "p.json", sleep=lambda s: None, backoff=(0, 0, 0))


def test_list_docs_filters_follows_index_cap_and_pdf_discovery(tmp_path, server):
    s = server({})
    s.files.update({"/sitemap.xml": index(s.base), "/sm1.xml": urlset(s.base), "/sm2.xml": urlset(s.base),
                    "/sm3.xml": urlset(s.base), "/sm4.xml": b"<urlset><url><loc>http://never</loc></url></urlset>",
                    PAGES[0][0]: MEETING_PAGE})
    cfg = {"type": "sitemap", "sitemap_url": f"{s.base}/sitemap.xml", "pattern": PATTERN, "extractor": "agenda_items",
           "follow_pages": 1, "doc_pattern": r"\.pdf|getmedia|/files/"}
    council, fetcher = fetcher_for(tmp_path, s.base)
    refs = sitemap.list_docs(council, cfg, fetcher)
    assert "/sm4.xml" not in s.hits and "/sm3.xml" in s.hits                      # max 3 children
    urls = [r.url.replace(s.base, "") for r in refs]
    assert urls == ["/getmedia/abc/Agenda-17-Nov-2026.pdf", "/files/att.pdf", PAGES[1][0], PAGES[2][0]]
    pdf = refs[0]
    assert (pdf.doc_type, pdf.meeting_date, pdf.fmt, pdf.extractor) == ("agenda", "2026-11-17", "pdf", "agenda_items")
    assert refs[2].fmt == "html" and refs[2].doc_type == "minutes" and refs[2].meeting_date == "2026-10-20"
    assert PAGES[1][0] not in s.hits                                              # only the newest page is fetched


def test_page_without_pdf_falls_back_to_page_ref(tmp_path, server):
    s = server({})
    s.files.update({"/sitemap.xml": urlset(s.base), PAGES[0][0]: b"<html><a href='/x'>x</a></html>"})
    cfg = {"sitemap_url": f"{s.base}/sitemap.xml", "pattern": PATTERN, "follow_pages": 1, "doc_pattern": r"\.pdf"}
    council, fetcher = fetcher_for(tmp_path, s.base)
    refs = sitemap.list_docs(council, cfg, fetcher)
    assert len(refs) == 3 and refs[0].url.endswith("17-november-2026") and refs[0].fmt == "html"


# ---- fallback wiring ----------------------------------------------------------------------------
def fallback_council(tmp_path, s, primary):
    raw = {"type": primary, "extractor": "agenda_items",
           "fallback": {"type": "sitemap", "sitemap_url": f"{s.base}/sitemap.xml", "pattern": PATTERN,
                        "follow_pages": 1, "doc_pattern": r"\.pdf|getmedia"}}
    return fetcher_for(tmp_path, s.base, [raw])


def test_fallback_when_adapter_raises(tmp_path, server, monkeypatch):
    s = server({})
    s.files.update({"/sitemap.xml": urlset(s.base), PAGES[0][0]: MEETING_PAGE})
    council, fetcher = fallback_council(tmp_path, s, "kentico_ksearch")
    from radar.adapters import kentico_ksearch

    def boom(*a, **k):
        raise KeyError("MNAFirstFileUrl")
    monkeypatch.setattr(kentico_ksearch, "list_docs", boom)
    result = CouncilResult("melville")
    refs = list_refs(council, fetcher, result)
    assert len(refs) == 4 and all(r.adapter == "sitemap" and r.extractor == "agenda_items" for r in refs)
    assert refs[0].fmt == "pdf" and refs[0].meeting_date == "2026-11-17"
    assert len(result.errors) == 1 and "adapter kentico_ksearch failed (KeyError: 'MNAFirstFileUrl')" in result.errors[0]
    assert "used sitemap fallback (4 docs)" in result.errors[0] and result.notes == []
    # `radar list` shares the logic
    assert len(adapters.list_council(council, fetcher)) == 4


def test_fallback_when_adapter_lists_nothing_records_note(tmp_path, server, monkeypatch):
    s = server({})
    s.files.update({"/sitemap.xml": urlset(s.base)})
    council, fetcher = fallback_council(tmp_path, s, "kentico_ksearch")
    from radar.adapters import kentico_ksearch
    monkeypatch.setattr(kentico_ksearch, "list_docs", lambda *a, **k: [])
    result = CouncilResult("melville")
    refs = list_refs(council, fetcher, result)
    assert len(refs) == 3 and result.errors == []
    assert result.notes == ["adapter kentico_ksearch listed no documents; used sitemap fallback (3 docs)"]


def test_no_fallback_still_records_error(tmp_path, server, monkeypatch):
    s = server({})
    council, fetcher = fetcher_for(tmp_path, s.base, [{"type": "kentico_ksearch"}])
    from radar.adapters import kentico_ksearch
    monkeypatch.setattr(kentico_ksearch, "list_docs", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad")))
    result = CouncilResult("melville")
    assert list_refs(council, fetcher, result) == []
    assert result.errors == ["adapter kentico_ksearch: ValueError: bad"]


def test_config_fallbacks_are_valid():
    from radar.settings import load_councils
    for key in ("melville", "stirling", "rockingham"):
        fb = next(a.raw for a in load_councils().councils[key].adapters if a.type == "kentico_ksearch")["fallback"]
        assert fb["type"] == "sitemap" and fb["sitemap_url"].endswith("/sitemap.xml") and fb["pattern"]


# ---- program totals -----------------------------------------------------------------------------
T1 = datetime(2026, 11, 17, 1, 0, tzinfo=timezone.utc)
ROAD = [{"code": "ROAD_RESURF", "method": "rule", "evidence": "kw"}]
NONCIV = [{"code": "NON_CIVIL", "method": "rule", "evidence": "kw"}]


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    d = SourceDocument(doc_id="a" * 64, doc_key="https://j.test/b.pdf", council_id="joondalup", source_url="https://j.test/b.pdf",
                       doc_type="budget", retrieved_at="2026-11-17T01:00:00+00:00", content_type="application/pdf")
    db.upsert_source_document(c, d)
    rows = [("1", "Road A", 100000, ROAD, "adopted_budget"), ("2", "Pool", 500000, NONCIV, "adopted_budget"),
            ("3", "Road B (no value)", None, ROAD, None), ("4", "Road C deferred", 70000, ROAD, "deferred"),
            ("5", "Road D", 25000, ROAD, "adopted_budget")]
    for a, name, v, labels, stage in rows:
        db.upsert_mention(c, ProjectMention(
            mention_id=make_mention_id(d.doc_id, "t", a), doc_id=d.doc_id, council_id="joondalup", page=1, char_start=0,
            char_end=1, project_name_raw=name, extractor="t", value_amount=v, value_basis="budget_total", stage=stage,
            financial_year="2026-27", trade_labels=labels, quote="q"))
    run = changes.start_run(c, ["joondalup"], now=T1)
    dedupe.link(c)
    changes.record_changes(c, run, now=T1)
    changes.finish_run(c, run, now=T1)
    return c


def test_program_totals_values(conn):
    rows = digest.program_totals(digest.load_data(conn))
    assert len(rows) == 1
    r = rows[0]
    assert (r["council_id"], r["financial_year"], r["projects"], r["no_value"]) == ("joondalup", "2026-27", 4, 1)
    assert r["total_all"] == 625000 and r["total_civil"] == 125000 and r["civil_projects"] == 3


def test_digest_renders_totals_and_note(conn):
    html = digest.render_html(digest.build_context(conn, now=T1))
    assert "Extracted capital program totals" in html and "$625,000" in html and "$125,000" in html
    assert "not the council&#39;s statutory budget total. GST basis may vary." in html


def test_export_has_program_totals(conn):
    payload = export.build_data(conn, now=T1)
    assert payload["program_totals"][0]["total_all"] == 625000
    assert "statutory budget total" in payload["program_totals_note"]
    js = (export.UI_SOURCE_DIR / "app.js").read_text()
    assert "program_totals" in js and "innerHTML" not in js.split("program_totals", 1)[1][:600]
