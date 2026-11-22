"""The polite fetcher (PLAN.md §2.1 [2], §9). The only module that touches the network.

Enforces, in order, for every request (and every redirect hop): global blocklist, host pause,
robots.txt, per-host rate limit. Then: honest UA, conditional GET, optional HEAD, streaming size
cap, challenge detection (pause host 24 h, never retry with different headers), fetch log, and the
``.cache/raw/<host>/<sha256>.<ext>`` store.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

from radar import db
from radar.models import FetchLogEntry, SourceDocument
from radar.ratelimit import HostPauses, HostRateLimiter
from radar.robots import RobotsCache, host_key
from radar.settings import (
    BACKOFF_SECONDS,
    CACHE_DIR,
    FETCH_LOG_PATH,
    HOST_PAUSE_PATH,
    LARGE_THRESHOLD_BYTES,
    LISTING_MAX_BYTES,
    MAX_BYTES,
    USER_AGENT,
    Blocklist,
    CouncilsConfig,
    load_councils,
)

MAX_REDIRECTS = 5
HEAD_FIRST_ABOVE_BYTES = 10 * 1024 * 1024  # PLAN §9.5
CHALLENGE_SNIFF_BYTES = 64 * 1024
# Specific markers only, to avoid false positives on ordinary pages (PLAN §9.4).
CHALLENGE_MARKERS = (
    b"cf-browser-verification",
    b"cf-challenge",
    b"challenge-platform",
    b"attention required! | cloudflare",
    b"<title>just a moment...</title>",
    b"sucuri website firewall",
    b"cloudproxy@sucuri.net",
    b"errors.edgesuite.net",
    b"_incapsula_resource",
)

# Outcomes
OK = "ok"
NOT_MODIFIED = "not_modified"
BLOCKED = "blocked"
ROBOTS_DISALLOWED = "robots_disallowed"
PAUSED = "paused"
SKIPPED_SIZE = "skipped_size"
CHALLENGE = "challenge"
HTTP_ERROR = "http_error"
ERROR = "error"


@dataclass
class FetchResult:
    url: str
    outcome: str
    status: int | None = None
    final_url: str | None = None
    path: Path | None = None
    sha256: str | None = None
    bytes: int | None = None
    etag: str | None = None
    last_modified: str | None = None
    content_type: str | None = None
    from_cache: bool = False
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome in (OK, NOT_MODIFIED)

    def read_bytes(self) -> bytes:
        if not self.path:
            raise FileNotFoundError(f"no cached body for {self.url} ({self.outcome})")
        return self.path.read_bytes()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def blocklist_match(url: str, blocklist: list[Blocklist], blocked_hosts: set[str] = frozenset()) -> str | None:
    """Return a reason string if ``url`` is blocked by the configured lists (defence in depth, PLAN §3.4)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host in {h.lower() for h in blocked_hosts}:
        return f"blocklist:{host}"
    for entry in blocklist:
        if host != entry.host.lower():
            continue
        if entry.path_prefix is None or parts.path.startswith(entry.path_prefix):
            return f"blocklist:{host}{entry.path_prefix or ''}"
    return None


def _guess_ext(content_type: str | None, url: str) -> str:
    if content_type:
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if ext:
            return ext.lstrip(".")
    suffix = Path(urlsplit(url).path).suffix.lstrip(".").lower()
    return suffix if suffix and len(suffix) <= 5 else "bin"


class Fetcher:
    def __init__(
        self,
        *,
        config: CouncilsConfig | None = None,
        conn: sqlite3.Connection | None = None,
        cache_dir: Path | None = None,
        log_path: Path | None = FETCH_LOG_PATH,
        pause_path: Path | None = HOST_PAUSE_PATH,
        session: requests.Session | None = None,
        limiter: HostRateLimiter | None = None,
        pauses: HostPauses | None = None,
        robots: RobotsCache | None = None,
        user_agent: str = USER_AGENT,
        max_bytes: int = MAX_BYTES,
        large_threshold: int = LARGE_THRESHOLD_BYTES,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        backoff: tuple[float, ...] = BACKOFF_SECONDS,
        timeout: tuple[float, float] = (10, 60),
    ) -> None:
        self.config = config or load_councils()
        self.conn = conn
        self.cache_dir = Path(cache_dir) if cache_dir else CACHE_DIR
        self.log_path = Path(log_path) if log_path else None
        self.user_agent = user_agent
        self.session = session or requests.Session()
        self.limiter = limiter or HostRateLimiter(self.config.defaults.min_delay_s, clock=clock, sleep=sleep)
        self.pauses = pauses or HostPauses(pause_path)
        self.robots = robots or RobotsCache(self._robots_get, self.cache_dir / "robots", user_agent=user_agent)
        self.max_bytes = max_bytes
        self.large_threshold = large_threshold
        self._sleep = sleep
        self.backoff = backoff
        self.timeout = timeout
        self._blocked_hosts = {h for c in self.config.councils.values() for h in c.blocked_hosts}
        self.paused_this_run: dict[str, str] = {}

    # ---- helpers ---------------------------------------------------------------------------
    def raw_path(self, url: str, sha256: str, content_type: str | None) -> Path:
        host = host_key(url).replace(":", "_")
        return self.cache_dir / "raw" / host / f"{sha256}.{_guess_ext(content_type, url)}"

    def cached_raw(self, sha256: str, url: str | None = None) -> Path | None:
        """Cached raw bytes for ``sha256``: the URL's host folder first, then any host (redirects store bytes under
        the final host, e.g. www.mainroads.wa.gov.au -> assets.mainroads.wa.gov.au)."""
        hit = self._cached_by_sha(url, sha256) if url else None
        if hit is None and (self.cache_dir / "raw").exists():
            hit = next(iter(sorted((self.cache_dir / "raw").glob(f"*/{sha256}.*"))), None)
        return hit

    def _cached_by_sha(self, url: str, sha256: str) -> Path | None:
        folder = self.cache_dir / "raw" / host_key(url).replace(":", "_")
        matches = sorted(folder.glob(f"{sha256}.*")) if folder.exists() else []
        return matches[0] if matches else None

    def _log(self, method: str, url: str, *, status=None, nbytes=None, etag=None, last_modified=None,
             started: float | None = None, robots="", cache_hit=False, note=None) -> None:
        elapsed = int((time.monotonic() - started) * 1000) if started is not None else None
        entry = FetchLogEntry(
            ts=_now(), method=method, url=url, host=host_key(url), status=status, bytes=nbytes, etag=etag,
            last_modified=last_modified, elapsed_ms=elapsed, robots_decision=robots, cache_hit=cache_hit, note=note,
        )
        if self.conn is not None:
            db.insert_fetch_log(self.conn, entry)
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            row = {k: v for k, v in entry.__dict__.items() if k != "id"}
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, sort_keys=True) + "\n")

    def _pause(self, url: str, reason: str) -> None:
        host = host_key(url)
        self.pauses.pause(host, reason)
        self.paused_this_run[host] = reason

    def _gate(self, url: str, *, check_robots: bool = True) -> tuple[str | None, str | None, str]:
        """Return (outcome, reason, robots_decision). outcome None means go ahead."""
        why = blocklist_match(url, self.config.global_blocklist, self._blocked_hosts)
        if why:
            return BLOCKED, why, "blocklist"
        host = host_key(url)
        if self.pauses.is_paused(host):
            return PAUSED, f"paused:{self.pauses.reason(host)}", "paused"
        if not check_robots:
            return None, None, "n/a"
        allowed, decision = self.robots.can_fetch(url)
        if not allowed:
            return ROBOTS_DISALLOWED, decision, decision
        delay = self.robots.crawl_delay(url)
        if delay:
            self.limiter.set_crawl_delay(host, delay)
        return None, None, decision

    def _robots_get(self, robots_url: str) -> tuple[int | None, str]:
        """Fetch robots.txt itself: blocklist/pause/rate-limit apply, robots does not (it is the source)."""
        outcome, reason, _ = self._gate(robots_url, check_robots=False)
        if outcome:
            return None, ""
        started = time.monotonic()
        try:
            self.limiter.wait(host_key(robots_url))
            resp = self.session.get(
                robots_url, headers={"User-Agent": self.user_agent}, timeout=self.timeout,
                allow_redirects=False, stream=True,
            )
            body = resp.raw.read(512 * 1024, decode_content=True)
            resp.close()
            if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                loc = urljoin(robots_url, resp.headers["Location"])
                if host_key(loc) == host_key(robots_url):  # same-host redirect only
                    self._log("GET", robots_url, status=resp.status_code, started=started, robots="n/a", note="robots redirect")
                    return self._robots_get(loc)
            self._log("GET", robots_url, status=resp.status_code, nbytes=len(body), started=started, robots="n/a",
                      note="robots.txt")
            return resp.status_code, body.decode("utf-8", errors="replace")
        except requests.RequestException as exc:
            self._log("GET", robots_url, started=started, robots="n/a", note=f"robots error: {type(exc).__name__}")
            return None, ""

    def _challenge_in(self, resp: requests.Response, first_chunk: bytes) -> str | None:
        if resp.headers.get("cf-mitigated", "").lower() == "challenge":
            return "cf-mitigated"
        ctype = resp.headers.get("Content-Type", "").lower()
        if "html" in ctype or "text" in ctype:
            low = first_chunk[:CHALLENGE_SNIFF_BYTES].lower()
            for marker in CHALLENGE_MARKERS:
                if marker in low:
                    return marker.decode()
        return None

    # ---- public API ------------------------------------------------------------------------
    def head(self, url: str) -> FetchResult:
        """HEAD request (same gates). ``bytes`` is the declared Content-Length, if any."""
        return self._request("HEAD", url)

    def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        json_body: object | None = None,
        headers: dict[str, str] | None = None,
        prior: SourceDocument | None = None,
        max_bytes: int | None = None,
        allow_large: bool = False,
        head_first: bool | None = None,
    ) -> FetchResult:
        """GET/POST ``url`` politely. ``prior`` supplies validators for a conditional GET (GET only)."""
        if head_first is None:
            head_first = bool(prior and prior.bytes and prior.bytes > HEAD_FIRST_ABOVE_BYTES)
        if head_first and method == "GET":
            h = self.head(url)
            if not h.ok:
                return h
            declared = h.bytes
            if declared is not None:
                skip = self._size_verdict(declared, max_bytes, allow_large)
                if skip:
                    return FetchResult(url, SKIPPED_SIZE, status=h.status, bytes=declared, reason=skip)
        return self._request(method, url, json_body=json_body, headers=headers, prior=prior,
                             max_bytes=max_bytes, allow_large=allow_large)

    def _size_verdict(self, declared: int, max_bytes: int | None, allow_large: bool) -> str | None:
        cap = max_bytes if max_bytes is not None else self.max_bytes
        if declared > cap:
            return f"content-length {declared} > cap {cap}"
        if max_bytes is None and declared > self.large_threshold and not allow_large:
            return f"content-length {declared} > large threshold {self.large_threshold} (use --allow-large)"
        return None

    def _request(self, method, url, *, json_body=None, headers=None, prior=None, max_bytes=None,
                 allow_large=False) -> FetchResult:
        cap = max_bytes if max_bytes is not None else self.max_bytes
        current = url
        for hop in range(MAX_REDIRECTS + 1):
            outcome, reason, robots_decision = self._gate(current)
            if outcome:
                self._log(method, current, robots=robots_decision, note=reason)
                return FetchResult(url, outcome, reason=reason, final_url=current)

            req_headers = {"User-Agent": self.user_agent, "Accept": "*/*"}
            if headers:
                req_headers.update(headers)
            if method == "GET" and prior and hop == 0:
                if prior.http_etag:
                    req_headers["If-None-Match"] = prior.http_etag
                if prior.http_last_modified:
                    req_headers["If-Modified-Since"] = prior.http_last_modified

            resp = None
            started = time.monotonic()
            for attempt in range(len(self.backoff) + 1):
                self.limiter.wait(host_key(current))
                started = time.monotonic()
                try:
                    resp = self.session.request(
                        method, current, headers=req_headers, json=json_body if method == "POST" else None,
                        timeout=self.timeout, allow_redirects=False, stream=True,
                    )
                except requests.RequestException as exc:
                    transient = isinstance(exc, (requests.Timeout, requests.ConnectionError))
                    if transient and attempt == 0 and self.backoff:     # one polite retry for a stalled server
                        self._log(method, current, started=started, robots=robots_decision,
                                  note=f"error: {type(exc).__name__}; retry in {self.backoff[0]}s")
                        self._sleep(self.backoff[0])
                        continue
                    self._log(method, current, started=started, robots=robots_decision,
                              note=f"error: {type(exc).__name__}: {exc}")
                    return FetchResult(url, ERROR, reason=f"{type(exc).__name__}: {exc}", final_url=current)
                if resp.status_code >= 500 and attempt < len(self.backoff):
                    self._log(method, current, status=resp.status_code, started=started, robots=robots_decision,
                              note=f"retry in {self.backoff[attempt]}s")
                    resp.close()
                    self._sleep(self.backoff[attempt])
                    continue
                break
            assert resp is not None

            status = resp.status_code
            etag = resp.headers.get("ETag")
            last_mod = resp.headers.get("Last-Modified")
            ctype = resp.headers.get("Content-Type")

            if status in (403, 429):
                resp.close()
                self._pause(current, f"http {status}")
                self._log(method, current, status=status, started=started, robots=robots_decision, note="host paused")
                return FetchResult(url, CHALLENGE if resp.headers.get("cf-mitigated") else PAUSED, status=status,
                                   final_url=current, reason=f"http {status}: host paused 24h")

            if status in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                nxt = urljoin(current, resp.headers["Location"])
                resp.close()
                self._log(method, current, status=status, started=started, robots=robots_decision, note=f"redirect -> {nxt}")
                current = nxt
                if method == "POST" and status in (301, 302, 303):
                    method, json_body = "GET", None
                continue

            if status == 304:
                resp.close()
                cached = self._cached_by_sha(current, prior.doc_id) if prior else None
                self._log(method, current, status=304, started=started, robots=robots_decision, cache_hit=True,
                          etag=etag, last_modified=last_mod)
                return FetchResult(url, NOT_MODIFIED, status=304, final_url=current, path=cached,
                                   sha256=prior.doc_id if prior else None, from_cache=True,
                                   etag=etag or (prior.http_etag if prior else None),
                                   last_modified=last_mod or (prior.http_last_modified if prior else None),
                                   content_type=prior.content_type if prior else None)

            if method == "HEAD":
                resp.close()
                length = resp.headers.get("Content-Length")
                self._log("HEAD", current, status=status, nbytes=int(length) if length and length.isdigit() else None,
                          started=started, robots=robots_decision, etag=etag, last_modified=last_mod)
                return FetchResult(url, OK if status < 400 else HTTP_ERROR, status=status, final_url=current,
                                   bytes=int(length) if length and length.isdigit() else None, etag=etag,
                                   last_modified=last_mod, content_type=ctype)

            if status >= 400:
                resp.close()
                self._log(method, current, status=status, started=started, robots=robots_decision)
                return FetchResult(url, HTTP_ERROR, status=status, final_url=current, reason=f"http {status}")

            return self._download(url, current, method, resp, started, robots_decision, cap, max_bytes, allow_large)
        self._log(method, current, robots="n/a", note="too many redirects")
        return FetchResult(url, ERROR, reason="too many redirects", final_url=current)

    def _download(self, url, current, method, resp, started, robots_decision, cap, max_bytes, allow_large) -> FetchResult:
        status = resp.status_code
        etag = resp.headers.get("ETag")
        last_mod = resp.headers.get("Last-Modified")
        ctype = resp.headers.get("Content-Type")
        declared = resp.headers.get("Content-Length")
        if declared and declared.isdigit():
            skip = self._size_verdict(int(declared), max_bytes, allow_large)
            if skip:
                resp.close()
                self._log(method, current, status=status, nbytes=int(declared), started=started,
                          robots=robots_decision, etag=etag, last_modified=last_mod, note=f"skipped_size: {skip}")
                return FetchResult(url, SKIPPED_SIZE, status=status, final_url=current, bytes=int(declared), reason=skip)

        # Streaming download with a running counter: aborts at the cap even without Content-Length.
        self.cache_dir.joinpath("raw").mkdir(parents=True, exist_ok=True)
        tmp = self.cache_dir / "raw" / f".part-{host_key(current).replace(':', '_')}-{int(time.time() * 1000)}"
        hasher = hashlib.sha256()
        total = 0
        first = b""
        try:
            with tmp.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    if not first:
                        first = chunk
                        marker = self._challenge_in(resp, first)
                        if marker:
                            raise _Challenge(marker)
                    total += len(chunk)
                    if total > cap:
                        raise _TooBig()
                    hasher.update(chunk)
                    fh.write(chunk)
        except _TooBig:
            resp.close()
            tmp.unlink(missing_ok=True)
            self._log(method, current, status=status, nbytes=total, started=started, robots=robots_decision,
                      note=f"skipped_size: exceeded cap {cap} while streaming")
            return FetchResult(url, SKIPPED_SIZE, status=status, final_url=current, bytes=total,
                               reason=f"exceeded cap {cap} while streaming")
        except _Challenge as ch:
            resp.close()
            tmp.unlink(missing_ok=True)
            self._pause(current, f"challenge page ({ch})")
            self._log(method, current, status=status, started=started, robots=robots_decision,
                      note=f"challenge: {ch}; host paused")
            return FetchResult(url, CHALLENGE, status=status, final_url=current,
                               reason=f"challenge page ({ch}): host paused 24h")
        except requests.RequestException as exc:
            resp.close()
            tmp.unlink(missing_ok=True)
            self._log(method, current, status=status, started=started, robots=robots_decision,
                      note=f"error: {type(exc).__name__}")
            return FetchResult(url, ERROR, status=status, final_url=current, reason=f"{type(exc).__name__}: {exc}")
        resp.close()

        sha = hasher.hexdigest()
        dest = self.raw_path(current, sha, ctype)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp.replace(dest)
        self._log(method, current, status=status, nbytes=total, started=started, robots=robots_decision,
                  etag=etag, last_modified=last_mod)
        return FetchResult(url, OK, status=status, final_url=current, path=dest, sha256=sha, bytes=total,
                           etag=etag, last_modified=last_mod, content_type=ctype)

    # ---- maintenance -----------------------------------------------------------------------
    def prune_raw(self, days: int = 30, now: float | None = None) -> int:
        """Delete raw cache files older than ``days`` (PLAN §9.9). Returns files removed."""
        cutoff = (now if now is not None else time.time()) - days * 86400
        removed = 0
        raw = self.cache_dir / "raw"
        if raw.exists():
            for p in raw.rglob("*"):
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
        return removed


class _TooBig(Exception):
    pass


class _Challenge(Exception):
    pass
