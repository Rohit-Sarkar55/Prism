from app.health import ProviderHealth


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def tracker(clock, **kw):
    return ProviderHealth(
        error_rate_threshold=0.5,
        window_s=60,
        min_samples=4,
        max_consecutive_failures=5,
        probe_interval_s=5,
        clock=clock,
        **kw,
    )


def fail(h, n=4, name="alpha"):
    for _ in range(n):
        h.record(name, ok=False)


def test_a_provider_is_healthy_until_there_are_enough_samples():
    h = tracker(Clock())
    fail(h, 3)  # 100% errors but only 3 samples: noise, not a trend
    assert h.should_skip("alpha") is False


def test_it_degrades_once_the_error_rate_crosses_the_threshold():
    h = tracker(Clock())
    for ok in (True, True, False, False):  # exactly 50%
        h.record("alpha", ok)
    assert h.should_skip("alpha") is True
    assert h.snapshot()["alpha"]["state"] == "degraded"


def test_mostly_successful_traffic_stays_healthy():
    h = tracker(Clock())
    for ok in (True, True, True, False):  # 25%
        h.record("alpha", ok)
    assert h.should_skip("alpha") is False


def test_providers_are_tracked_independently():
    h = tracker(Clock())
    fail(h, 6, "alpha")
    assert h.should_skip("alpha") and not h.should_skip("beta")


def test_old_errors_age_out_of_the_window():
    clock = Clock()
    h = tracker(clock)
    fail(h)
    clock.now += 61
    assert h.should_skip("alpha") is False


def test_one_probe_is_allowed_per_interval_while_degraded():
    clock = Clock()
    h = tracker(clock)
    fail(h)
    assert h.should_skip("alpha") is True  # just failed: skip
    clock.now += 5
    assert h.should_skip("alpha") is False  # probe due: let one request through
    assert h.should_skip("alpha") is True  # the probe slot is taken: everyone else skips
    clock.now += 5
    assert h.should_skip("alpha") is False  # next interval, next probe


def test_a_successful_probe_closes_the_circuit_immediately():
    clock = Clock()
    h = tracker(clock)
    fail(h)
    clock.now += 5
    assert h.should_skip("alpha") is False  # the probe goes out...
    h.record("alpha", ok=True)  # ...and succeeds
    assert h.should_skip("alpha") is False
    assert h.snapshot()["alpha"]["state"] == "healthy"


def test_a_failed_probe_keeps_the_circuit_open():
    clock = Clock()
    h = tracker(clock)
    fail(h)
    clock.now += 5
    assert h.should_skip("alpha") is False
    h.record("alpha", ok=False)
    assert h.should_skip("alpha") is True


def test_from_config_reads_the_provided_degradation_block():
    h = ProviderHealth.from_config({"degradation": {"error_rate_threshold": 0.8, "window_seconds": 30}})
    assert (h.error_rate_threshold, h.window_s) == (0.8, 30)


def test_a_hard_down_provider_degrades_after_consecutive_failures_even_with_few_samples():
    h = ProviderHealth(clock=Clock())  # defaults: 10 samples for the rate rule, 5 in a row for the fast rule
    fail(h, 4)
    assert h.should_skip("alpha") is False
    fail(h, 1)
    assert h.should_skip("alpha") is True


def test_a_success_resets_the_consecutive_run():
    h = ProviderHealth(clock=Clock())
    for ok in (False, False, False, False, True, False, False, False, False):
        h.record("alpha", ok)
    assert h.should_skip("alpha") is False  # never 5 failures in a row, and only 9 samples


def test_a_flaky_provider_is_not_written_off_on_a_small_sample():
    h = ProviderHealth(clock=Clock())
    for ok in (False, True, False, True, False, False, True, True):  # 50% over 8 calls, never 5 in a row
        h.record("alpha", ok)
    assert h.should_skip("alpha") is False  # fewer than 10 samples: not yet a trend


def test_a_sustained_high_error_rate_still_degrades_with_enough_samples():
    h = ProviderHealth(clock=Clock())
    for ok in (False, True) * 5:  # 10 calls, 50% errors, never consecutive
        h.record("alpha", ok)
    assert h.should_skip("alpha") is True