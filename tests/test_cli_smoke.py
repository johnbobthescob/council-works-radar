"""M16: offline end-to-end smoke test of ``run`` against a local http.server (PLAN §11.2)."""

import json
import re
import threading
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from radar import cli, db, export
from radar.adapters.base import DocRef
from radar.fetch import Fetcher
from radar.pipeline import run_pipeline, select_docs
from radar.settings import Adapter, Council, CouncilsConfig, Defaults

FIX = Path(__file__).parent / "fixtures"
CWP = (FIX / "melville_cwp_snippet.html").read_bytes()
NOW = datetime(2026, 11, 22, 3, 0, tzinfo=timezone.utc)
ETAG = '"cwp-v1"'


class Srv:
    def __init__(self):
        self.hits = []
        self.httpd = None

    def start(self):
        hits = self.hits

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                hits.append((self.command, self.path))
                if self.path == "/robots.txt":
                    body, code, extra = b"User-agent: *\nAllow: /\n", 200, {"Content-Type": "text/plain"}
                elif self.path == "/cwp":
                    if self.headers.get("If-None-Match") == ETAG:
                        self.send_response(304)
                        self.send_header("ETag", ETAG)
                        self.end_headers()
                        return
                    body, code, extra = CWP, 200, {"Content-Type": "text/html; charset=utf-8", "ETag": ETAG}
                else:
                    body, code, extra = b"nope", 404, {"Content-Type": "text/plain"}
                self.send_response(code)
                for k, v in extra.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        return self

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None


@pytest.fixture
def srv():
    s = Srv().start()
    yield s
    s.stop()


def make_config(base: str) -> CouncilsConfig:
    host = base.split("//")[1]

    def council(key, name, adapters):
        return Council(key=key, name=name, enabled=True, hosts=[host], blocked_hosts=[], terms_url=None,
                       adapters=[Adapter(a["type"], a) for a in adapters])

    return CouncilsConfig(
        defaults=Defaults(min_delay_s=0.0),
        global_blocklist=[],
        councils={
            "melville": council("melville", "City of Melville", [{
                "type": "static_links",
                "docs": [{"url": f"{base}/cwp", "doc_type": "capital_works_program", "format": "html",
                          "extractor": "melville_cwp"}]}]),
            "cockburn": council("cockburn", "City of Cockburn", [{
                "type": "static_links",
                "docs": [{"url": f"{base}/missing.pdf", "doc_type": "project_plans", "financial_year": "2026-27",
                          "format": "pdf", "extractor": "cockburn_plans"}]}]),
            "broken": council("broken", "Broken Council", [{"type": "no_such_adapter"}]),
        })


def do_run(tmp_path, srv, councils, *, offline=False, config=None):
    config = config or make_config(srv.base)
    conn = db.connect(tmp_path / "r.sqlite")
    fetcher = Fetcher(config=config, conn=conn, cache_dir=tmp_path / "cache", log_path=tmp_path / "fetch.jsonl",
                      pause_path=tmp_path / "pause.json", sleep=lambda s: None, backoff=(0, 0, 0))
    try:
        return run_pipeline(conn, councils, fetcher=fetcher, config=config, out_dir=tmp_path / "out",
                            data_dir=tmp_path / "data", offline=offline, now=NOW)
    finally:
        conn.close()


def test_a_full_run_writes_outputs(tmp_path, srv):
    res = do_run(tmp_path, srv, ["melville"])
    out = tmp_path / "out"
    wk = res.iso_week
    assert (out / f"digest-{wk}.html").exists()
    if res.summary["outputs"]["pdf_error"] is None:
        assert (out / f"digest-{wk}.pdf").stat().st_size > 0
    for name in ("index.html", "app.js", "style.css", "data.js"):
        assert (out / "ui" / name).exists()
    summary = json.loads((out / f"run-summary-{wk}.json").read_text())
    assert summary["mode"] == "live" and summary["baseline"] is True
    m = summary["councils"]["melville"]
    assert m["status"] == "ok" and m["mentions_extracted"] > 0 and m["outcomes"] == {"extracted": 1}
    assert summary["totals"]["projects"] > 0
    assert (tmp_path / "data" / "listings" / "melville").is_dir()

    data = export.parse_data_js((out / "ui" / "data.js").read_text())
    projects = data["projects"]
    assert projects
    for p in projects:
        assert p["sources"], p
        for s in p["sources"]:
            assert s["url"].startswith("http://127.0.0.1")
            assert s.get("retrieved_at")

    html = (out / f"digest-{wk}.html").read_text()
    assert 'href="http://127.0.0.1' in html
    assert re.search(r"retrieved 20\d\d-\d\d-\d\d", html)
    assert "date unknown" not in html


def test_b_second_run_is_not_modified(tmp_path, srv):
    do_run(tmp_path, srv, ["melville"])
    res = do_run(tmp_path, srv, ["melville"])
    assert res.summary["baseline"] is False
    m = res.summary["councils"]["melville"]
    assert m["documents"][0]["outcome"] == "not_modified" and m["status"] == "ok"
    assert "new" not in res.summary["totals"]["events_by_type"]
    assert any(h[1] == "/cwp" for h in srv.hits)


def test_c_offline_replay_without_server(tmp_path, srv):
    first = do_run(tmp_path, srv, ["melville"])
    n = first.summary["totals"]["projects"]
    srv.stop()
    res = do_run(tmp_path, srv, ["melville"], offline=True)
    assert res.summary["mode"] == "offline-replay"
    m = res.summary["councils"]["melville"]
    assert m["outcomes"] == {"extracted": 1} and m["mentions_extracted"] > 0
    assert "replayed from cache" in m["documents"][0]["detail"]
    assert res.summary["totals"]["projects"] == n


def test_d_failure_isolation(tmp_path, srv):
    res = do_run(tmp_path, srv, ["melville", "cockburn", "broken"])
    c = res.summary["councils"]
    assert c["melville"]["status"] == "ok"
    assert c["cockburn"]["status"] == "failed" and c["cockburn"]["documents"][0]["outcome"] == "skipped"
    assert any("http_error" in e for e in c["cockburn"]["errors"])     # a 404 is a recorded failure
    assert c["broken"]["status"] == "failed" and c["broken"]["errors"]
    assert any(e["council"] == "broken" for e in res.summary["errors"])
    assert (tmp_path / "out" / f"run-summary-{res.iso_week}.json").exists()


def ref(url, doc_type, date_=None, mtype=None, extractor="agenda_items"):
    return DocRef(council_id="x", url=f"https://x.example/{url}", doc_type=doc_type, meeting_date=date_,
                  meeting_type=mtype, extractor=extractor)


def test_e_select_docs():
    today = date(2026, 11, 22)
    refs = [
        ref("old.pdf", "minutes", "2026-08-01", "Ordinary Council Meeting"),           # outside 60 days
        ref("m1-min.pdf", "minutes", "2026-11-10", "Ordinary Council Meeting"),
        ref("m1-ag.pdf", "agenda", "2026-11-10", "Ordinary Council Meeting"),           # minutes preferred
        ref("m2-ag.pdf", "agenda", "2026-10-20", "Ordinary Council Meeting"),
        ref("m3-ag.pdf", "agenda", "2026-10-01", "Ordinary Council Meeting"),           # over cap
        ref("budget.pdf", "budget", None, extractor="joondalup_budget"),
        ref("nox.pdf", "budget", None, extractor="not_implemented"),
        ref("noext.pdf", "budget", None, extractor=None),
    ]
    sel, skipped = select_docs(refs, today=today, window_days=60, max_meeting_docs=2)
    urls = {r.url.rsplit("/", 1)[1] for r in sel}
    assert urls == {"budget.pdf", "m1-min.pdf", "m2-ag.pdf"}
    why = {r.url.rsplit("/", 1)[1]: w for r, w in skipped}
    assert "outside" in why["old.pdf"]
    assert "minutes" in why["m1-ag.pdf"]
    assert "cap" in why["m3-ag.pdf"]
    assert "no implemented extractor" in why["nox.pdf"] and "no implemented extractor" in why["noext.pdf"]


def test_f_cli_unknown_council(tmp_path, capsys):
    rc = cli.main(["run", "--councils", "nope", "--db", str(tmp_path / "x.sqlite"), "--out", str(tmp_path / "o")])
    assert rc == 2
    assert "unknown council" in capsys.readouterr().err


def test_select_docs_skips_landing_pages():
    from datetime import date

    from radar.adapters.base import DocRef
    from radar.pipeline import select_docs

    page = DocRef(council_id="melville", url="https://x/agenda-ordinary-meeting-17-november-2026", doc_type="agenda",
                  meeting_date="2026-11-17", extractor="agenda_items", landing=True)
    selected, skipped = select_docs([page], today=date(2026, 11, 22), window_days=60, max_meeting_docs=2)
    assert selected == [] and "landing page" in skipped[0][1]
