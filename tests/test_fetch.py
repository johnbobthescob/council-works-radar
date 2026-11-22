"""M2: polite fetcher, robots, rate limiter, host pause. Offline: a local http.server + fake clock."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from radar import db, fetch as F
from radar.models import SourceDocument
from radar.ratelimit import HostPauses, HostRateLimiter
from radar.robots import RobotsCache
from radar.settings import CouncilsConfig, Defaults, Blocklist


class State:
    def __init__(self):
        self.robots = "User-agent: *\nDisallow: /private/\n"
        self.robots_status = 200
        self.hits = []          # (method, path, headers)
        self.body = b"hello pdf bytes"
        self.etag = '"v1"'
        self.big = 0
        self.mode = None        # None | "403" | "challenge" | "500"


@pytest.fixture
def server():
    state = State()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _handle(self, send_body=True):
            state.hits.append((self.command, self.path, dict(self.headers)))
            if self.path == "/robots.txt":
                self.send_response(state.robots_status)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(state.robots.encode())
                return
            if state.mode == "403":
                self.send_response(403); self.end_headers(); return
            if state.mode == "500":
                self.send_response(500); self.end_headers(); return
            if state.mode == "challenge":
                body = b"<html><title>Just a moment...</title>cf-browser-verification</html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body); return
            if self.path == "/redirect-private":
                self.send_response(302); self.send_header("Location", "/private/x"); self.end_headers(); return
            if self.path.startswith("/big"):
                n = state.big
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                if "chunked" not in self.path:
                    self.send_header("Content-Length", str(n))
                self.end_headers()
                if send_body:
                    try:
                        sent = 0
                        while sent < n:
                            self.wfile.write(b"x" * min(8192, n - sent)); sent += 8192
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                return
            if self.headers.get("If-None-Match") == state.etag:
                self.send_response(304); self.send_header("ETag", state.etag); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("ETag", state.etag)
            self.send_header("Content-Length", str(len(state.body)))
            self.end_headers()
            if send_body:
                self.wfile.write(state.body)

        def do_GET(self): self._handle()
        def do_HEAD(self): self._handle(send_body=False)
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self._handle()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    state.base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield state
    srv.shutdown()


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


@pytest.fixture
def clock():
    return FakeClock()


def make_fetcher(tmp_path, clock, conn=None, blocklist=(), **kw):
    cfg = CouncilsConfig(defaults=Defaults(min_delay_s=5), councils={}, global_blocklist=list(blocklist))
    return F.Fetcher(
        config=cfg, conn=conn, cache_dir=tmp_path / "cache", log_path=tmp_path / "fetch_log.jsonl",
        pause_path=tmp_path / "pause.json", clock=clock.now, sleep=clock.sleep, **kw,
    )


def test_network_guard_blocks_internet():
    with pytest.raises(Exception, match="blocked"):
        requests.get("http://example.com/", timeout=2)


def test_user_agent_and_cache_file(server, tmp_path, clock):
    f = make_fetcher(tmp_path, clock)
    r = f.fetch(server.base + "/doc.pdf")
    assert r.outcome == F.OK and r.read_bytes() == server.body
    assert r.path.parent.parent.name == "raw" and r.path.name == f"{r.sha256}.pdf"
    doc_hits = [h for h in server.hits if h[1] == "/doc.pdf"]
    assert doc_hits[0][2]["User-Agent"].startswith("CouncilWorksRadar/")
    assert "Mozilla" not in doc_hits[0][2]["User-Agent"]


def test_robots_disallow_respected(server, tmp_path, clock):
    f = make_fetcher(tmp_path, clock)
    r = f.fetch(server.base + "/private/secret.pdf")
    assert r.outcome == F.ROBOTS_DISALLOWED
    assert not [h for h in server.hits if h[1].startswith("/private/")]
    # redirect into a disallowed path is re-checked per hop
    r2 = f.fetch(server.base + "/redirect-private")
    assert r2.outcome == F.ROBOTS_DISALLOWED
    assert not [h for h in server.hits if h[1].startswith("/private/")]


@pytest.mark.parametrize("status,allowed", [(404, True), (401, False), (403, False), (500, False)])
def test_robots_status_policy(server, tmp_path, clock, status, allowed):
    server.robots_status = status
    f = make_fetcher(tmp_path, clock)
    r = f.fetch(server.base + "/doc.pdf")
    assert (r.outcome == F.OK) is allowed
    if status == 500:
        assert not list((tmp_path / "cache" / "robots").glob("*"))  # transient: not cached on disk


def test_robots_cached_on_disk_and_ttl(server, tmp_path, clock):
    f = make_fetcher(tmp_path, clock)
    f.fetch(server.base + "/a.pdf")
    f2 = make_fetcher(tmp_path, clock)   # new process, same cache dir
    f2.fetch(server.base + "/b.pdf")
    assert len([h for h in server.hits if h[1] == "/robots.txt"]) == 1


def test_blocklist_enforced_without_any_request(server, tmp_path, clock):
    host = server.base.split("//")[1].split(":")[0]
    f = make_fetcher(tmp_path, clock, blocklist=[Blocklist(host=host, path_prefix="/v1/")])
    assert f.fetch(server.base + "/v1/x").outcome == F.BLOCKED
    assert server.hits == []
    assert f.fetch(server.base + "/ok.pdf").outcome == F.OK


def test_blocklist_match_rules():
    bl = [Blocklist(host="cdn.canning.wa.gov.au"), Blocklist(host="www.swan.wa.gov.au", path_prefix="/v1/")]
    assert F.blocklist_match("https://cdn.canning.wa.gov.au/x.pdf", bl)
    assert F.blocklist_match("https://www.swan.wa.gov.au/v1/a", bl)
    assert not F.blocklist_match("https://www.swan.wa.gov.au/about", bl)
    assert F.blocklist_match("https://search.joondalup.wa.gov.au/", [], {"search.joondalup.wa.gov.au"})


def test_conditional_get_304_reuses_cache(server, tmp_path, clock):
    conn = db.connect(":memory:")
    f = make_fetcher(tmp_path, clock, conn=conn)
    url = server.base + "/doc.pdf"
    r1 = f.fetch(url)
    prior = SourceDocument(doc_id=r1.sha256, doc_key=url, council_id="x", source_url=url, http_etag=r1.etag,
                           bytes=r1.bytes, content_type=r1.content_type)
    r2 = f.fetch(url, prior=prior)
    assert r2.outcome == F.NOT_MODIFIED and r2.from_cache and r2.status == 304
    assert r2.read_bytes() == server.body and r2.path == r1.path
    assert [h for h in server.hits if h[1] == "/doc.pdf"][-1][2]["If-None-Match"] == '"v1"'
    rows = db.fetch_log_rows(conn)
    assert any(r.cache_hit and r.status == 304 for r in rows)
    # changed ETag -> full download, new sha
    server.etag, server.body = '"v2"', b"new bytes"
    r3 = f.fetch(url, prior=prior)
    assert r3.outcome == F.OK and r3.sha256 != r1.sha256


def test_size_cap_content_length_skips_before_body(server, tmp_path, clock):
    server.big = 5000
    f = make_fetcher(tmp_path, clock, max_bytes=1000)
    r = f.fetch(server.base + "/big")
    assert r.outcome == F.SKIPPED_SIZE and r.path is None
    assert not list((tmp_path / "cache" / "raw").rglob("*.pdf"))


def test_size_cap_aborts_streaming_without_content_length(server, tmp_path, clock):
    server.big = 500_000
    f = make_fetcher(tmp_path, clock, max_bytes=100_000)
    r = f.fetch(server.base + "/big-chunked")   # no Content-Length header
    assert r.outcome == F.SKIPPED_SIZE and "streaming" in r.reason
    leftovers = [p for p in (tmp_path / "cache" / "raw").rglob("*") if p.is_file()]
    assert leftovers == []   # partial file removed


def test_large_threshold_requires_allow_large(server, tmp_path, clock):
    server.big = 3000
    f = make_fetcher(tmp_path, clock, max_bytes=10_000, large_threshold=1000)
    assert f.fetch(server.base + "/big").outcome == F.SKIPPED_SIZE
    assert f.fetch(server.base + "/big", allow_large=True).outcome == F.OK


def test_head_first_skips_without_get(server, tmp_path, clock):
    server.big = 5000
    f = make_fetcher(tmp_path, clock, max_bytes=1000)
    r = f.fetch(server.base + "/big", head_first=True)
    assert r.outcome == F.SKIPPED_SIZE
    methods = [h[0] for h in server.hits if h[1] == "/big"]
    assert methods == ["HEAD"]


def test_same_host_requests_at_least_delay_apart(server, tmp_path, clock):
    f = make_fetcher(tmp_path, clock)
    stamps = []
    orig = f.session.request

    def spy(*a, **kw):
        stamps.append(clock.now())
        return orig(*a, **kw)

    f.session.request = spy
    for i in range(3):
        assert f.fetch(server.base + f"/d{i}.pdf").outcome == F.OK
    assert all(b - a >= 5 for a, b in zip(stamps, stamps[1:])), stamps
    assert len(stamps) == 4   # robots.txt + 3 documents, all spaced


def test_rate_limiter_unit():
    c = FakeClock()
    rl = HostRateLimiter(5, clock=c.now, sleep=c.sleep)
    assert rl.wait("a") == 0
    assert rl.wait("b") == 0            # other host unaffected
    c.t += 2
    assert rl.wait("a") == pytest.approx(3)
    rl.set_crawl_delay("a", 20)
    assert rl.delay_for("a") == 20 and rl.delay_for("b") == 5


def test_robots_crawl_delay_honoured(server, tmp_path, clock):
    server.robots = "User-agent: *\nCrawl-delay: 12\n"
    f = make_fetcher(tmp_path, clock)
    f.fetch(server.base + "/a.pdf")
    f.fetch(server.base + "/b.pdf")
    assert max(clock.sleeps) >= 12 - 1e-6 or any(s >= 7 for s in clock.sleeps)
    assert f.limiter.delay_for(server.base.split("//")[1]) == 12


def test_403_pauses_host_and_persists(server, tmp_path, clock):
    server.mode = "403"
    f = make_fetcher(tmp_path, clock)
    r = f.fetch(server.base + "/a.pdf")
    assert r.outcome == F.PAUSED and r.status == 403
    n = len(server.hits)
    server.mode = None
    r2 = f.fetch(server.base + "/b.pdf")
    assert r2.outcome == F.PAUSED and len(server.hits) == n    # no request while paused
    assert f.paused_this_run
    # persisted for a fresh fetcher; clears after 24 h
    f2 = make_fetcher(tmp_path, clock)
    assert f2.fetch(server.base + "/c.pdf").outcome == F.PAUSED
    pauses = HostPauses(tmp_path / "pause.json", clock=lambda: 10**10)
    assert not pauses.is_paused(server.base.split("//")[1])


def test_challenge_page_pauses_host(server, tmp_path, clock):
    server.mode = "challenge"
    f = make_fetcher(tmp_path, clock)
    r = f.fetch(server.base + "/page.html")
    assert r.outcome == F.CHALLENGE and r.path is None
    assert f.pauses.is_paused(server.base.split("//")[1])


def test_5xx_backoff_then_error(server, tmp_path, clock):
    server.mode = "500"
    f = make_fetcher(tmp_path, clock)
    r = f.fetch(server.base + "/a.pdf")
    assert r.outcome == F.HTTP_ERROR and r.status == 500
    assert len([h for h in server.hits if h[1] == "/a.pdf"]) == 4     # 1 + 3 retries
    assert [s for s in clock.sleeps if s in (10, 30, 90)] == [10, 30, 90]


def test_fetch_log_written_to_jsonl_and_db(server, tmp_path, clock):
    conn = db.connect(":memory:")
    f = make_fetcher(tmp_path, clock, conn=conn)
    f.fetch(server.base + "/a.pdf")
    lines = (tmp_path / "fetch_log.jsonl").read_text().strip().splitlines()
    assert len(lines) >= 2                                             # robots + document
    rows = db.fetch_log_rows(conn)
    doc = [r for r in rows if r.url.endswith("/a.pdf")][0]
    assert doc.status == 200 and doc.bytes == len(server.body) and doc.robots_decision.startswith("robots_ok")


def test_post_json_listing(server, tmp_path, clock):
    f = make_fetcher(tmp_path, clock)
    r = f.fetch(server.base + "/list", method="POST", json_body={"take": 12})
    assert r.outcome == F.OK
    assert any(h[0] == "POST" for h in server.hits)


def test_prune_raw(server, tmp_path, clock):
    import os
    f = make_fetcher(tmp_path, clock)
    r = f.fetch(server.base + "/a.pdf")
    os.utime(r.path, (1, 1))
    assert f.prune_raw(30) == 1 and not r.path.exists()


def test_timeout_retried_once_then_succeeds(server, tmp_path, clock):
    f = make_fetcher(tmp_path, clock)
    real, calls = f.session.request, []

    def flaky(method, url, *a, **kw):
        if url.endswith("/robots.txt"):
            return real(method, url, *a, **kw)
        calls.append(url)
        if len(calls) == 1:
            raise requests.ReadTimeout("stalled")
        return real(method, url, *a, **kw)

    f.session.request = flaky
    r = f.fetch(server.base + "/a.pdf")
    assert r.outcome == F.OK and len(calls) == 2 and 10 in clock.sleeps


def test_timeout_twice_gives_error_without_more_retries(server, tmp_path, clock):
    f = make_fetcher(tmp_path, clock)
    real, calls = f.session.request, []

    def dead(method, url, *a, **kw):
        if url.endswith("/robots.txt"):
            return real(method, url, *a, **kw)
        calls.append(1)
        raise requests.ConnectTimeout("down")

    f.session.request = dead
    r = f.fetch(server.base + "/a.pdf")
    assert r.outcome == F.ERROR and len(calls) == 2
