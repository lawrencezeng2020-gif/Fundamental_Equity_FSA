"""Thread-safe token-bucket rate limiter for outbound SEC requests.

PLANNING.md Section 4: cap at ``settings.rate_limit_rps`` (default 5; SEC's
own policy limit is 10 req/s). The clock and sleep function are injected so
this is unit-testable deterministically -- a test proving the cap holds must
not actually sleep in real time (see ``tests/test_sec_ratelimit.py``).
"""

from __future__ import annotations

import threading
import time
from typing import Callable


class TokenBucketLimiter:
    """Classic token bucket: tokens refill continuously at ``rate`` tokens/sec,
    up to ``capacity``. ``acquire()`` blocks (via the injected ``sleep``) until
    a token is available, then consumes one.

    Thread-safe: a single instance is meant to be shared across a process
    (or, in this project, one per ``SecClient``), guarding it with a lock so
    concurrent callers still cap the aggregate rate correctly.
    """

    def __init__(
        self,
        rate: float,
        *,
        capacity: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError(f"rate must be positive, got {rate}")
        self._rate = float(rate)
        # Burst capacity defaults to the rate itself, i.e. up to one second's
        # worth of requests may fire back-to-back before throttling kicks in.
        self._capacity = float(capacity) if capacity is not None else float(rate)
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._tokens = self._capacity
        self._last_refill = clock()
        # Tolerance for floating-point refill/wait round-tripping (elapsed *
        # rate then / rate should return exactly to the deficit, but isn't
        # guaranteed bit-for-bit) so acquire() converges in one extra loop at
        # most instead of spinning on sub-epsilon shortfalls.
        self._epsilon = 1e-9

    def acquire(self) -> None:
        """Block until a single token is available, then consume it."""
        while True:
            with self._lock:
                now = self._clock()
                elapsed = max(0.0, now - self._last_refill)
                self._last_refill = now
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                if self._tokens >= 1.0 - self._epsilon:
                    self._tokens = max(0.0, self._tokens - 1.0)
                    return
                deficit = 1.0 - self._tokens
                wait_seconds = deficit / self._rate
            # Sleep outside the lock so other threads' refill accounting
            # isn't blocked while this one waits.
            self._sleep(wait_seconds)
