"""The retry / failover policy, one rule per test. No network, no real sleeping."""

import pytest

from app.errors import GatewayError
from app.health import ProviderHealth
from app.providers import ProviderError, ProviderRegistry
from app.providers.fake import FakeAdapter
from app.upstream import RetryPolicy, Upstream

CHAIN = ["alpha-small", "beta-small"]
MESSAGES = [{"role": "user", "content": "hi"}]


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def build(alpha=None, beta=None, health=None, policy=None, clock=None):
    alpha, beta = alpha or FakeAdapter("alpha"), beta or FakeAdapter("beta")
    sleeps: list[float] = []

    async def sleep(seconds):
        sleeps.append(seconds)
        if clock:
            clock.now += seconds

    upstream = Upstream(
        ProviderRegistry({"alpha": alpha, "beta": beta}),
        health or ProviderHealth(),
        policy or RetryPolicy(),
        sleep=sleep,
        rng=lambda: 0.5,  # jitter factor exactly 1.0, so delays are predictable
        **({"clock": clock} if clock else {}),
    )
    return upstream, alpha, beta, sleeps


def error(kind, status=None, transient=True):
    return ProviderError("alpha", kind, status=status, transient=transient)


async def test_healthy_primary_serves_with_no_retries():
    upstream, _alpha, beta, sleeps = build()
    out = await upstream.chat(CHAIN, MESSAGES, {})
    assert (out.provider, out.fallback, out.retries) == ("alpha", False, 0)
    assert beta.calls == [] and sleeps == []


async def test_transient_5xx_is_retried_on_the_same_provider_with_exponential_backoff():
    upstream, _alpha, beta, sleeps = build(alpha=FakeAdapter("alpha", fail_times=2))
    out = await upstream.chat(CHAIN, MESSAGES, {})
    assert (out.provider, out.fallback, out.retries) == ("alpha", False, 2)
    assert sleeps == pytest.approx([0.2, 0.4])  # 200ms, then x2
    assert beta.calls == []


async def test_backoff_is_jittered_within_25_percent():
    policy = RetryPolicy(initial_backoff_ms=200)
    assert policy.backoff_s(1, lambda: 0.0) == pytest.approx(0.15)
    assert policy.backoff_s(1, lambda: 1.0) == pytest.approx(0.25)
    assert policy.backoff_s(3, lambda: 0.5) == pytest.approx(0.8)


async def test_persistent_failure_exhausts_attempts_then_fails_over_and_flags_it():
    upstream, alpha, _beta, sleeps = build(alpha=FakeAdapter("alpha", fail_times=99))
    out = await upstream.chat(CHAIN, MESSAGES, {})
    assert (out.provider, out.model, out.fallback) == ("beta", "beta-small", True)
    assert len(alpha.calls) == 3  # max_attempts
    assert out.retries == 3  # 4 attempts in total
    assert len(sleeps) == 2


@pytest.mark.parametrize(
    "kind", ["connect", "timeout", "rate_limited", "malformed_response"]
)  # retrying these on the same provider only burns the caller's time
async def test_these_failures_fail_over_immediately_without_retrying(kind):
    upstream, alpha, _beta, sleeps = build(alpha=FakeAdapter("alpha", fail_times=99, error=error(kind)))
    out = await upstream.chat(CHAIN, MESSAGES, {})
    assert (out.provider, out.fallback, out.retries) == ("beta", True, 1)
    assert len(alpha.calls) == 1 and sleeps == []


async def test_network_blip_is_retried_like_a_5xx():
    upstream, _alpha, _, sleeps = build(alpha=FakeAdapter("alpha", fail_times=1, error=error("network")))
    out = await upstream.chat(CHAIN, MESSAGES, {})
    assert (out.provider, out.retries) == ("alpha", 1) and len(sleeps) == 1


@pytest.mark.parametrize("status", [400, 413, 422])
async def test_a_request_the_provider_rejects_as_bad_is_not_failed_over(status):
    upstream, _alpha, beta, _ = build(
        alpha=FakeAdapter("alpha", fail_times=99, error=error("client_error", status, False))
    )
    with pytest.raises(GatewayError) as exc:
        await upstream.chat(CHAIN, MESSAGES, {})
    assert exc.value.status == 502
    assert beta.calls == []  # the same bad request would fail there too: do not spend another provider's quota


@pytest.mark.parametrize("status", [401, 403, 404])
async def test_provider_side_client_errors_do_fail_over(status):
    upstream, alpha, _beta, _ = build(
        alpha=FakeAdapter("alpha", fail_times=99, error=error("client_error", status, False))
    )
    out = await upstream.chat(CHAIN, MESSAGES, {})
    assert (out.provider, out.fallback) == ("beta", True)
    assert len(alpha.calls) == 1  # no retry either


async def test_every_provider_failing_is_a_502_that_reports_the_attempts():
    upstream, *_ = build(alpha=FakeAdapter("alpha", fail_times=99), beta=FakeAdapter("beta", fail_times=99))
    with pytest.raises(GatewayError) as exc:
        await upstream.chat(CHAIN, MESSAGES, {})
    assert exc.value.status == 502 and exc.value.type == "upstream_error"
    assert exc.value.retries == 5  # 3 attempts on alpha + 3 on beta, minus the first
    assert "alpha" not in exc.value.message and "beta" not in exc.value.message


async def test_the_overall_deadline_stops_retrying():
    clock = Clock()
    policy = RetryPolicy(max_attempts=5, initial_backoff_ms=400, deadline_s=0.5)
    upstream, alpha, _beta, _ = build(alpha=FakeAdapter("alpha", fail_times=99), policy=policy, clock=clock)
    with pytest.raises(GatewayError):
        await upstream.chat(["alpha-small"], MESSAGES, {})
    assert len(alpha.calls) == 2  # the sleep after attempt 2 pushed the clock past the deadline


async def test_a_degraded_provider_is_skipped_straight_to_the_fallback():
    health = ProviderHealth(min_samples=2)
    for _ in range(3):
        health.record("alpha", ok=False)
    upstream, alpha, _beta, sleeps = build(health=health)
    out = await upstream.chat(CHAIN, MESSAGES, {})
    assert (out.provider, out.fallback, out.retries) == ("beta", True, 0)
    assert alpha.calls == [] and sleeps == []


async def test_the_last_model_in_the_chain_is_tried_even_if_degraded():
    health = ProviderHealth(min_samples=2)
    for _ in range(3):
        health.record("alpha", ok=False)
    upstream, alpha, *_ = build(health=health)
    out = await upstream.chat(["alpha-small"], MESSAGES, {})  # nothing after it to fall back to
    assert out.provider == "alpha" and len(alpha.calls) == 1


async def test_outcomes_feed_the_health_tracker():
    health = ProviderHealth()
    upstream, *_ = build(alpha=FakeAdapter("alpha", fail_times=99), health=health)
    for _ in range(2):
        await upstream.chat(CHAIN, MESSAGES, {})
    assert health.snapshot()["alpha"]["state"] == "degraded"  # 6 failures in a row
    assert health.snapshot()["beta"]["state"] == "healthy"


# ---- streams: same walk, but only until the first chunk ---------------------------------


async def test_stream_fails_over_when_the_primary_fails_before_its_first_chunk():
    upstream, _alpha, _beta, _ = build(alpha=FakeAdapter("alpha", fail_times=99, error=error("connect")))
    opened = await upstream.open_stream(CHAIN, MESSAGES, {})
    assert (opened.provider, opened.fallback) == ("beta", True)
    assert opened.first.data["choices"]
    await opened.aclose()


async def test_stream_retries_a_transient_failure_on_the_same_provider():
    upstream, _alpha, _beta, sleeps = build(alpha=FakeAdapter("alpha", fail_times=1))
    opened = await upstream.open_stream(CHAIN, MESSAGES, {})
    assert (opened.provider, opened.fallback, opened.retries) == ("alpha", False, 1)
    assert len(sleeps) == 1
    await opened.aclose()


async def test_stream_where_every_provider_fails_is_a_502():
    upstream, *_ = build(alpha=FakeAdapter("alpha", fail_times=99), beta=FakeAdapter("beta", fail_times=99))
    with pytest.raises(GatewayError) as exc:
        await upstream.open_stream(CHAIN, MESSAGES, {})
    assert exc.value.status == 502