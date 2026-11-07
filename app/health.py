"""Provider health tracking: a small circuit breaker driven by recent error rate.

A provider is "degraded" and gets skipped (the gateway goes straight to the fallback, instead
of making every request wait through retries and timeouts) when EITHER:
  * a run of consecutive failures shows it is hard down (default 5 in a row), or
  * at least `min_samples` recent calls exist and the error rate is over the threshold.
The sample minimum matters: with only a few samples, a flaky-but-working provider (say 30%
errors) crosses 50% by chance and would be written off while retries could still absorb it.
While it is degraded, one probe request is let through every `probe_interval_s`. A successful
probe closes the circuit at once, so a recovered provider is used again within seconds.
"""

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class _State:
    events: deque = field(default_factory=deque)  # (time, ok)
    last_probe: float = float("-inf")


class ProviderHealth:
    def __init__(
        self,
        *,
        error_rate_threshold: float = 0.5,
        window_s: float = 60.0,
        min_samples: int = 10,
        max_consecutive_failures: int = 5,
        probe_interval_s: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.error_rate_threshold = error_rate_threshold
        self.window_s = window_s
        self.min_samples = min_samples  # too few samples is noise, not a trend
        self.max_consecutive_failures = max_consecutive_failures
        self.probe_interval_s = probe_interval_s
        self._clock = clock
        self._states: dict[str, _State] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, config: dict, **overrides) -> "ProviderHealth":
        degradation = config.get("degradation", {})
        return cls(
            error_rate_threshold=degradation.get("error_rate_threshold", 0.5),
            window_s=degradation.get("window_seconds", 60),
            **overrides,
        )

    # ---- internals (call with the lock held) ---------------------------------------------

    def _state(self, provider: str) -> _State:
        state = self._states.setdefault(provider, _State())
        cutoff = self._clock() - self.window_s
        while state.events and state.events[0][0] <= cutoff:
            state.events.popleft()
        return state

    def _error_rate(self, state: _State) -> float:
        return sum(1 for _, ok in state.events if not ok) / len(state.events) if state.events else 0.0

    def _trailing_failures(self, state: _State) -> int:
        count = 0
        for _, ok in reversed(state.events):
            if ok or count >= self.max_consecutive_failures:
                break
            count += 1
        return count

    def _degraded(self, state: _State) -> bool:
        if self._trailing_failures(state) >= self.max_consecutive_failures:
            return True  # hard down: no need to wait for a statistically meaningful sample
        return len(state.events) >= self.min_samples and self._error_rate(state) >= self.error_rate_threshold

    # ---- public API ----------------------------------------------------------------------

    def record(self, provider: str, ok: bool) -> None:
        with self._lock:
            state = self._state(provider)
            was_degraded = self._degraded(state)
            state.events.append((self._clock(), ok))
            if ok and was_degraded:
                state.events.clear()  # a successful probe: close the circuit immediately

    def should_skip(self, provider: str) -> bool:
        """True if the provider is degraded and no probe is due. When a probe IS due, this
        claims the probe slot (so only one request probes per interval) and returns False.
        """
        with self._lock:
            state = self._state(provider)
            if not self._degraded(state):
                return False
            last_activity = max(state.events[-1][0], state.last_probe)
            if self._clock() - last_activity >= self.probe_interval_s:
                state.last_probe = self._clock()
                return False
            return True

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            out = {}
            for name in self._states:
                state = self._state(name)
                out[name] = {
                    "state": "degraded" if self._degraded(state) else "healthy",
                    "recent_calls": len(state.events),
                    "recent_error_rate": round(self._error_rate(state), 3),
                }
            return out