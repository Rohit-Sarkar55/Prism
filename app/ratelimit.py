"""Per-key requests-per-minute limit: a sliding-window log, race-safe by construction.

`acquire` checks the window and records the hit in ONE synchronous step (no `await` between
the read and the write, plus a lock for threads). That is what makes it safe under concurrent
requests: a read-then-write limiter lets several requests all see "9 of 10 used" and all
proceed, which is exactly the over-admission the load test looks for.

State is in memory, so it is per gateway process and resets on restart. That is acceptable
for a single instance; a multi-instance deployment would need a shared store (e.g. Redis).
"""

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after_s: float = 0.0  # when rejected: seconds until the oldest hit leaves the window


class SlidingWindowLimiter:
    def __init__(self, window_s: float = 60.0, clock: Callable[[], float] = time.monotonic):
        self.window_s = window_s
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def acquire(self, key: str, limit: int) -> Decision:
        """Admit and record one request for `key`, or reject it without recording anything."""
        now = self._clock()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            cutoff = now - self.window_s
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= limit:
                return Decision(False, max(hits[0] + self.window_s - now, 0.001))
            hits.append(now)
            return Decision(True)