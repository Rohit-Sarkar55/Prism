from concurrent.futures import ThreadPoolExecutor

from app.ratelimit import SlidingWindowLimiter


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_admits_up_to_the_limit_then_rejects_with_a_retry_hint():
    limiter = SlidingWindowLimiter(clock=Clock())
    assert all(limiter.acquire("k", 3).allowed for _ in range(3))
    rejected = limiter.acquire("k", 3)
    assert rejected.allowed is False
    assert 0 < rejected.retry_after_s <= 60


def test_rejected_requests_do_not_use_up_the_window():
    clock = Clock()
    limiter = SlidingWindowLimiter(clock=clock)
    limiter.acquire("k", 1)
    for _ in range(50):
        limiter.acquire("k", 1)  # hammering while limited must not extend the penalty
    clock.now += 60.1
    assert limiter.acquire("k", 1).allowed


def test_window_slides_instead_of_resetting_all_at_once():
    clock = Clock()
    limiter = SlidingWindowLimiter(window_s=60, clock=clock)
    limiter.acquire("k", 2)  # t=0
    clock.now += 30
    limiter.acquire("k", 2)  # t=30
    assert not limiter.acquire("k", 2).allowed
    clock.now += 31  # t=61: the first hit has left the window, the second has not
    assert limiter.acquire("k", 2).allowed
    assert not limiter.acquire("k", 2).allowed


def test_retry_after_points_at_the_oldest_hit_leaving():
    clock = Clock()
    limiter = SlidingWindowLimiter(window_s=60, clock=clock)
    limiter.acquire("k", 1)
    clock.now += 20
    assert round(limiter.acquire("k", 1).retry_after_s) == 40


def test_keys_are_limited_independently():
    limiter = SlidingWindowLimiter(clock=Clock())
    limiter.acquire("a", 1)
    assert not limiter.acquire("a", 1).allowed
    assert limiter.acquire("b", 1).allowed


def test_concurrent_callers_never_exceed_the_limit():
    limiter = SlidingWindowLimiter()  # real clock
    with ThreadPoolExecutor(max_workers=32) as pool:
        decisions = list(pool.map(lambda _: limiter.acquire("k", 10), range(400)))
    assert sum(d.allowed for d in decisions) == 10  # exactly the limit: no over-admission