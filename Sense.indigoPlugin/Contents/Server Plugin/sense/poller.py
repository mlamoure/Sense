"""Poll scheduling with per-source cadence and failure backoff. Pure state, no I/O, no Indigo.

Three sources, three cadences:
- realtime: total/appliance watts, every `realtime_interval` seconds (default 60);
- trends + discovered appliances: every `trend_interval` seconds (default 300);
- login: retried after `LOGIN_RETRY_AUTH` (needs the user) or `LOGIN_RETRY_TRANSIENT`.

A failed fetch doubles its wait (capped at `MAX_BACKOFF`) and a success resets it, so a
Sense outage costs a handful of requests an hour instead of a request per interval.
"""

from __future__ import annotations

import time

REALTIME_INTERVAL = 60
MIN_REALTIME_INTERVAL = 30
TREND_INTERVAL = 300
MAX_BACKOFF = 300
LOGIN_RETRY_AUTH = 900  # bad password / MFA needed: nothing changes until the user acts
LOGIN_RETRY_TRANSIENT = 60

REALTIME = "realtime"
TRENDS = "trends"
LOGIN = "login"


class Poller:
    def __init__(
        self,
        realtime_interval: int = REALTIME_INTERVAL,
        trend_interval: int = TREND_INTERVAL,
        clock=time.monotonic,
    ):
        self._clock = clock
        self._interval = {
            REALTIME: max(int(realtime_interval), MIN_REALTIME_INTERVAL),
            TRENDS: int(trend_interval),
            LOGIN: 0,
        }
        self._next = {k: 0.0 for k in self._interval}
        self._failures = {k: 0 for k in self._interval}

    def due(self, kind: str) -> bool:
        return self._clock() >= self._next[kind]

    def succeeded(self, kind: str) -> None:
        self._failures[kind] = 0
        self._next[kind] = self._clock() + self._interval[kind]

    def failed(self, kind: str, wait: float | None = None) -> float:
        """Record a failure; return the seconds until the next attempt."""
        self._failures[kind] += 1
        if wait is None:
            base = self._interval[kind] or LOGIN_RETRY_TRANSIENT
            wait = min(base * (2 ** (self._failures[kind] - 1)), MAX_BACKOFF)
        self._next[kind] = self._clock() + wait
        return wait

    def reset(self, kind: str) -> None:
        """Make `kind` due immediately (e.g. after a successful login)."""
        self._failures[kind] = 0
        self._next[kind] = 0.0

    def failures(self, kind: str) -> int:
        return self._failures[kind]

    def seconds_until(self, kind: str) -> float:
        return max(0.0, self._next[kind] - self._clock())
