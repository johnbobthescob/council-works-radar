"""M17: pipeline ``enable={'blackspot'}`` against a loopback server serving a tiny synthetic Black Spot PDF."""

import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pymupdf
import pytest

from radar import db, export
from radar.digest import BLACKSPOT_NOTE
from radar.fetch import Fetcher
from radar.pipeline import run_pipeline
from radar.settings import Adapter, Council, CouncilsConfig, Defaults, load_councils

CWP = (Path(__file__).parent / "fixtures" / "melville_cwp_snippet.html").read_bytes()
NOW = datetime(2026, 11, 22, 3, 0, tzinfo=timezone.utc)
LINES = ["2026-27 State Black Spot Program", "Ardross Street-Munro Road (City of Melville)", "Install roundabout",
         "$700,000", "Sorrento Road-Dundebar Road (Shire of Esperance)", "Turn lane", "$90,000"]


def make_pdf() -> bytes:
    pdf = pymupdf.open()
    page = pdf.new_page()
    y = 72
    for ln in LINES:
        page.insert_text((72, y), ln)
        y += 18
    data = pdf.tobytes()
    pdf.close()
    return data


@pytest.fixture
def srv():
    hits = []
    pdf = make_pdf()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits.append(self.path)
            table = {"/robots.txt": (b"User-agent: *\nAllow: /\n", "text/plain"), "/cwp": (CWP, "text/html; charset=utf-8"),
                     "/bs.pdf": (pdf, "application/pdf")}
            body, ctype = table.get(self.path, (b"nope", "text/plain"))
            self.send_response(200 if self.path in table else 404)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", hits
    httpd.shutdown()
    httpd.server_close()


def config(base):
    host = base.split("//")[1]
    melville = Council(key="melville", name="City of Melville", enabled=True, hosts=[host], blocked_hosts=[], terms_url=None,
                       adapters=[Adapter("static_links", {"type": "static_links", "docs": [
                           {"url": f"{base}/cwp", "doc_type": "capital_works_program", "format": "html",
                            "extractor": "melville_cwp"}]})])
    cross = {"blackspot": {"name": "Main Roads WA State Black Spot Program", "enabled": False, "hosts": [host], "docs": [
        {"url": f"{base}/bs.pdf", "doc_type": "blackspot_list", "financial_year": "2026-27", "extractor": "blackspot"}]}}
    return CouncilsConfig(defaults=Defaults(min_delay_s=0.0), councils={"melville": melville}, global_blocklist=[],
                          cross_checks=cross)


def run(tmp_path, base, **kw):
    cfg = config(base)
    conn = db.connect(tmp_path / "r.sqlite")
    fetcher = Fetcher(config=cfg, conn=conn, cache_dir=tmp_path / "cache", log_path=tmp_path / "f.jsonl",
                      pause_path=tmp_path / "p.json", sleep=lambda s: None, backoff=(0, 0, 0))
    try:
        return run_pipeline(conn, ["melville"], fetcher=fetcher, config=cfg, out_dir=tmp_path / "out",
                            data_dir=tmp_path / "data", pdf=False, now=NOW, **kw)
    finally:
        conn.close()


def test_disabled_fetches_nothing(tmp_path, srv):
    base, hits = srv
    res = run(tmp_path, base)
    assert "/bs.pdf" not in hits and "cross_checks" in res.summary and res.summary["cross_checks"] == {}
    assert BLACKSPOT_NOTE not in (tmp_path / "out" / f"digest-{res.iso_week}.html").read_text()
    data = export.parse_data_js((tmp_path / "out" / "ui" / "data.js").read_text())
    assert data["blackspot_note"] is None
    assert all("BlackSpot_State" not in p["funding"] or p["sources"] for p in data["projects"])


def test_enabled_tags_projects(tmp_path, srv):
    base, hits = srv
    res = run(tmp_path, base, enable={"blackspot"})
    assert "/bs.pdf" in hits
    cc = res.summary["cross_checks"]["blackspot"]
    assert cc["outcomes"] == {"extracted": 1} and cc["mentions_extracted"] == 1 and cc["projects_tagged"] == 1
    assert not res.summary["errors"]
    data = export.parse_data_js((tmp_path / "out" / "ui" / "data.js").read_text())
    assert data["blackspot_note"]
    tagged = [p for p in data["projects"] if any(s["extractor"] == "blackspot" for s in p["sources"])]
    assert len(tagged) == 1 and "ardross" in tagged[0]["name"].lower() and "BlackSpot_State" in tagged[0]["funding"]
    assert all(p["council"] == "melville" for p in data["projects"])
    html = (tmp_path / "out" / f"digest-{res.iso_week}.html").read_text()
    assert BLACKSPOT_NOTE[:40] in html
    assert json.loads((tmp_path / "out" / f"run-summary-{res.iso_week}.json").read_text())["cross_checks"]

    # second run: 304/unchanged-or-same-sha, still one tagged project and no duplicate links
    res2 = run(tmp_path, base, enable={"blackspot"})
    data2 = export.parse_data_js((tmp_path / "out" / "ui" / "data.js").read_text())
    assert len([p for p in data2["projects"] if any(s["extractor"] == "blackspot" for s in p["sources"])]) == 1
    assert res2.summary["totals"]["projects"] == res.summary["totals"]["projects"]


def test_shipped_config_is_disabled():
    cfg = load_councils()
    assert len(cfg.councils) == 6
    bs = cfg.cross_checks["blackspot"]
    assert bs["enabled"] is False and bs["docs"][0]["extractor"] == "blackspot"
