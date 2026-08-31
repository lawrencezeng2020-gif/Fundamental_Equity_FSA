"""Deterministic tests for TokenBucketLimiter -- no real sleeping.

The clock and sleep function are both faked and share the same mutable
"simulated time" counter, so `acquire()`'s internal waiting shows up as
advances to that counter rather than actual wall-clock delay. This is what
makes it possible to "prove the cap holds" without a slow, flaky,
real-time-based test.
"""

from __future__ import annotations

import threading
import time

import pytest

from fsa.sec.ratelimit import TokenBucketLimiter


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def time(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.t += seconds


def test_burst_up_to_capacity_costs_no_simulated_time():
    fake = FakeClock()
    limiter = TokenBucketLimiter(rate=5, clock=fake.time, sleep=fake.sleep)

    for _ in range(5):
        limiter.acquire()

    assert fake.t == 0.0


def test_requests_beyond_capacity_are_paced_at_the_configured_rate():
    fake = FakeClock()
    rate = 5.0
    limiter = TokenBucketLimiter(rate=rate, clock=fake.time, sleep=fake.sleep)

    n_requests = 23
    for _ in range(n_requests):
        limiter.acquire()

    # First `rate` requests are a free burst; each one after that costs
    # 1/rate simulated seconds. This is the deterministic proof that the cap
    # holds: no real time elapses, but the simulated clock advances exactly
    # as if `rate` requests/sec were enforced.
    expected = (n_requests - rate) / rate
    assert fake.t == pytest.approx(expected, rel=1e-6)


def test_steady_state_spacing_never_exceeds_the_cap():
    """The real proof the cap holds: once the initial burst is spent, every
    subsequent acquisition must be spaced at least 1/rate simulated seconds
    after the previous one -- not merely "close on average", which a burst
    at the start could mask over a short window."""
    fake = FakeClock()
    rate = 5.0
    limiter = TokenBucketLimiter(rate=rate, clock=fake.time, sleep=fake.sleep)

    timestamps = []
    for _ in range(100):
        limiter.acquire()
        timestamps.append(fake.t)

    min_spacing = 1.0 / rate
    # Skip the free burst (first `rate` acquisitions cost no time); every gap
    # after that must be >= min_spacing, with only floating-point slack.
    post_burst = timestamps[int(rate):]
    gaps = [b - a for a, b in zip(post_burst, post_burst[1:])]
    assert all(gap >= min_spacing - 1e-9 for gap in gaps)
    # And it shouldn't be *wildly* more than the cap either (no runaway
    # over-throttling bug).
    assert all(gap <= min_spacing + 1e-6 for gap in gaps)


def test_limiter_never_calls_real_sleep():
    fake = FakeClock()
    limiter = TokenBucketLimiter(rate=5, clock=fake.time, sleep=fake.sleep)

    start = time.monotonic()
    for _ in range(60):
        limiter.acquire()
    wall_clock_elapsed = time.monotonic() - start

    # At 5 req/s, 60 real acquisitions would take ~11s of real waiting if
    # this were backed by time.sleep. Fail fast if it ever is.
    assert wall_clock_elapsed < 1.0
    # ...while simulated time did advance, proving throttling actually ran.
    assert fake.t > 10.0


def test_rejects_non_positive_rate():
    with pytest.raises(ValueError):
        TokenBucketLimiter(rate=0)
    with pytest.raises(ValueError):
        TokenBucketLimiter(rate=-1)


def test_thread_safe_under_concurrent_acquire():
    fake = FakeClock()
    clock_lock = threading.Lock()

    def locked_time() -> float:
        with clock_lock:
            return fake.t

    def locked_sleep(seconds: float) -> None:
        with clock_lock:
            fake.t += seconds

    rate = 5.0
    limiter = TokenBucketLimiter(rate=rate, clock=locked_time, sleep=locked_sleep)

    n_per_thread = 15
    n_threads = 4

    def worker() -> None:
        for _ in range(n_per_thread):
            limiter.acquire()

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    total = n_per_thread * n_threads
    min_span = (total - rate) / rate
    # No deadlock/crash, and the aggregate rate across all threads still
    # respects the cap -- concurrent callers cannot evade the shared budget.
    assert fake.t >= min_span - 1e-6
