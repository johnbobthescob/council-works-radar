"""robots.txt handling (PLAN.md §9.2).

Policy: 404 -> allowed; 401/403 -> host disallowed; 5xx / network error -> disallowed for this run.
Successful and 404/401/403 answers are cached on disk for 24 h; transient failures are not cached
on disk so the next run retries.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from radar.settings import ROBOTS_TTL_SECONDS, USER_AGENT

# getter(robots_url) -> (status or None on network error, body text)
RobotsGetter = Callable[[str], tuple[int | None, str]]


def host_key(url: str) -> str:
    """Rate-limit / robots key: host plus explicit port (so local test servers stay distinct)."""
    return urlsplit(url).netloc.lower()


def _safe_name(key: str) -> str:
    return key.replace(":", "_").replace("/", "_")


class _Entry:
    def __init__(self, status: int | None, body: str, fetched_at: float) -> None:
        self.status = status
        self.body = body
        self.fetched_at = fetched_at
        self.parser: RobotFileParser | None = None
        # allow-all / disallow-all / parse
        if status is not None and 200 <= status < 300:
            self.mode = "parse"
            self.parser = RobotFileParser()
            self.parser.parse(body.splitlines())
        elif status == 404 or (status is not None and 400 <= status < 500 and status not in (401, 403, 429)):
            self.mode = "allow_all"
        else:
            # 401, 403, 429, 5xx, network error
            self.mode = "disallow_all"

    def allowed(self, user_agent: str, url: str) -> bool:
        if self.mode == "allow_all":
            return True
        if self.mode == "disallow_all":
            return False
        assert self.parser is not None
        return self.parser.can_fetch(user_agent, url)

    def crawl_delay(self, user_agent: str) -> float | None:
        if self.parser is None:
            return None
        delay = self.parser.crawl_delay(user_agent)
        return float(delay) if delay is not None else None

    @property
    def transient(self) -> bool:
        return self.status is None or self.status >= 500 or self.status == 429


class RobotsCache:
    def __init__(
        self,
        getter: RobotsGetter | None,
        cache_dir: Path | None = None,
        ttl: float = ROBOTS_TTL_SECONDS,
        user_agent: str = USER_AGENT,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._getter = getter
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.ttl = ttl
        self.user_agent = user_agent
        self._clock = clock
        self._mem: dict[str, _Entry] = {}

    def _load_disk(self, key: str) -> _Entry | None:
        if not self.cache_dir:
            return None
        meta = self.cache_dir / f"{_safe_name(key)}.json"
        body = self.cache_dir / f"{_safe_name(key)}.txt"
        if not meta.exists():
            return None
        try:
            info = json.loads(meta.read_text())
            text = body.read_text(encoding="utf-8") if body.exists() else ""
        except (OSError, ValueError):
            return None
        if self._clock() - info["fetched_at"] > self.ttl:
            return None
        return _Entry(info["status"], text, info["fetched_at"])

    def _save_disk(self, key: str, entry: _Entry) -> None:
        if not self.cache_dir or entry.transient:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / f"{_safe_name(key)}.txt").write_text(entry.body, encoding="utf-8")
        (self.cache_dir / f"{_safe_name(key)}.json").write_text(
            json.dumps({"status": entry.status, "fetched_at": entry.fetched_at})
        )

    def entry_for(self, url: str, *, offline: bool = False) -> _Entry | None:
        """Return the robots entry for ``url``'s host (fetching if needed). ``offline`` uses cache only."""
        key = host_key(url)
        if key in self._mem:
            return self._mem[key]
        entry = self._load_disk(key)
        if entry is None:
            if offline or self._getter is None:
                return None
            parts = urlsplit(url)
            status, body = self._getter(f"{parts.scheme}://{parts.netloc}/robots.txt")
            entry = _Entry(status, body if status is not None else "", self._clock())
            self._save_disk(key, entry)
        self._mem[key] = entry
        return entry

    def can_fetch(self, url: str, *, offline: bool = False) -> tuple[bool, str]:
        """Return (allowed, reason). Reason is a short string for the fetch log."""
        entry = self.entry_for(url, offline=offline)
        if entry is None:
            return False, "robots_unknown"
        if entry.allowed(self.user_agent, url):
            return True, f"robots_ok:{entry.status}"
        return False, f"robots_disallow:{entry.status}"

    def crawl_delay(self, url: str) -> float | None:
        entry = self._mem.get(host_key(url))
        return entry.crawl_delay(self.user_agent) if entry else None
