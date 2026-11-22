"""Per-host rate limiting and 24 h host pauses (PLAN.md §9.3, §9.4).

Both classes take injectable clocks so tests never really sleep.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

from radar.settings import HOST_PAUSE_SECONDS, MIN_DELAY_SECONDS


class HostRateLimiter:
    """Guarantees >= ``delay`` seconds between the *starts* of requests to the same host."""

    def __init__(
        self,
        min_delay: float = MIN_DELAY_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_delay = float(min_delay)
        self._clock = clock
        self._sleep = sleep
        self._last: dict[str, float] = {}
        self._crawl_delay: dict[str, float] = {}

    def set_crawl_delay(self, host: str, seconds: float | None) -> None:
        """Honour robots ``Crawl-delay`` when it is stricter than our floor."""
        if seconds and seconds > 0:
            self._crawl_delay[host] = float(seconds)

    def delay_for(self, host: str) -> float:
        return max(self.min_delay, self._crawl_delay.get(host, 0.0))

    def wait(self, host: str) -> float:
        """Block until a request to ``host`` is allowed; return seconds slept."""
        slept = 0.0
        last = self._last.get(host)
        if last is not None:
            remaining = self.delay_for(host) - (self._clock() - last)
            if remaining > 0:
                self._sleep(remaining)
                slept = remaining
        self._last[host] = self._clock()
        return slept


class HostPauses:
    """Persistent pause list (``data/host_pause.json``): a host that returned 403/429/challenge is left alone."""

    def __init__(
        self,
        path: Path | None = None,
        clock: Callable[[], float] = time.time,
        duration: float = HOST_PAUSE_SECONDS,
    ) -> None:
        self.path = Path(path) if path else None
        self._clock = clock
        self.duration = duration
        self._state: dict[str, dict] = {}
        if self.path and self.path.exists():
            try:
                self._state = json.loads(self.path.read_text())
            except (OSError, ValueError):
                self._state = {}

    def pause(self, host: str, reason: str) -> None:
        self._state[host] = {"until": self._clock() + self.duration, "reason": reason, "since": self._clock()}
        self._save()

    def remaining(self, host: str) -> float:
        entry = self._state.get(host)
        if not entry:
            return 0.0
        left = entry["until"] - self._clock()
        if left <= 0:
            self._state.pop(host, None)
            self._save()
            return 0.0
        return left

    def is_paused(self, host: str) -> bool:
        return self.remaining(host) > 0

    def reason(self, host: str) -> str | None:
        return self._state.get(host, {}).get("reason") if self.is_paused(host) else None

    def clear(self, host: str) -> None:
        if self._state.pop(host, None) is not None:
            self._save()

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._state, indent=2, sort_keys=True))
