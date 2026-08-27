"""
Routing and failover, tested offline against a scripted fake provider.

No network: each test scripts an exact sequence of upstream responses and
asserts the router's decisions. Live behaviour is covered separately by the
verify/probe path — here we need determinism, including for cases a live
provider will not reproduce on demand (a 429 with Retry-After, a 503 storm,
a circuit trip).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

import pytest

from oe_max.health import RetryPolicy, RouteHealth
from oe_max.providers.base import (
    RETRYABLE, ChatResult, ModelSpec, Outcome, ProviderAdapter, ProviderRole,
    looks_like_free_limit,
)
from oe_max.providers.registry import Registry
from oe_max.router import NoRouteAvailable, Router


class FakeProvider(ProviderAdapter):
    """Replays a scripted list of outcomes; records what it was asked."""

    def __init__(self, name: str, models: Dict[str, ModelSpec],
                 script: Optional[List[Outcome]] = None, **kw):
        super().__init__(name, "http://fake", models=models, requires_key=False, **kw)
        self.script = list(script or [])
        self.calls: List[Dict[str, Any]] = []

    async def chat(self, client, model_id, messages, *, attempt=1, **params) -> ChatResult:
        await self.limiter.acquire()
        self.calls.append({"model": model_id, "attempt": attempt, "params": params})
        outcome = self.script.pop(0) if self.script else Outcome.OK
        if outcome is Outcome.OK:
            return ChatResult(
                Outcome.OK, self.name, model_id, 10.0, status_code=200,
                body={"choices": [{"message": {"role": "assistant",
                                               "content": f"from {self.name}"}}],
                      "usage": {"total_tokens": 10}},
                attempt=attempt,
            )
        status = {Outcome.RATE_LIMITED: 429, Outcome.UNAVAILABLE: 503,
                  Outcome.AUTH_FAILED: 401, Outcome.BAD_REQUEST: 400,
                  Outcome.SERVER_ERROR: 500,
                  # A spent free allowance arrives as a 429 like any other rate
                  # limit — the status alone cannot tell them apart.
                  Outcome.FREE_LIMIT_EXHAUSTED: 429}.get(outcome, 500)
        return ChatResult(outcome, self.name, model_id, 5.0, status_code=status,
                          error=f"scripted {outcome.value}", attempt=attempt)


def spec(key, mid, **kw):
    return {key: ModelSpec(key=key, id=mid, **kw)}


def build(primary_script=None, fallback_script=None, **kw):
    primary = FakeProvider("primary", spec("ox_alpha", "ox-1"), primary_script)
    fallback = FakeProvider("fallback", spec("backup", "bk-1"), fallback_script)
    reg = Registry({"primary": primary, "fallback": fallback})
    router = Router(
        reg,
        chain=[("primary", "ox_alpha"), ("fallback", "backup")],
        retry=RetryPolicy(max_attempts=kw.pop("max_attempts", 3), base_delay_s=0.0,
                          jitter=False),
        health=RouteHealth(failure_threshold=kw.pop("failure_threshold", 3),
                           cooldown_seconds=kw.pop("cooldown", 45.0)),
    )
    return router, primary, fallback


@pytest.mark.asyncio
async def test_primary_is_preferred_when_healthy():
    router, primary, fallback = build()
    r = await router.chat(None, [{"role": "user", "content": "hi"}])
    assert r.ok and r.provider == "primary"
    assert len(fallback.calls) == 0, "fallback must not be touched when primary works"


@pytest.mark.asyncio
async def test_retries_then_succeeds_on_same_route():
    router, primary, fallback = build(
        primary_script=[Outcome.UNAVAILABLE, Outcome.UNAVAILABLE, Outcome.OK])
    r = await router.chat(None, [{"role": "user", "content": "hi"}])
    assert r.ok and r.provider == "primary" and r.attempt == 3
    assert len(primary.calls) == 3
    assert len(fallback.calls) == 0


@pytest.mark.asyncio
async def test_falls_over_after_exhausting_primary():
    router, primary, fallback = build(
        primary_script=[Outcome.UNAVAILABLE] * 3, max_attempts=3)
    r = await router.chat(None, [{"role": "user", "content": "hi"}])
    assert r.ok and r.provider == "fallback"
    assert len(primary.calls) == 3


@pytest.mark.asyncio
async def test_non_retryable_error_skips_straight_to_fallback():
    """
    A 400 means "this request/model is wrong", not "try again". Retrying it
    three times wastes the rate budget for a guaranteed failure — exactly what
    Zen returns for `Model is unavailable`.
    """
    router, primary, fallback = build(primary_script=[Outcome.BAD_REQUEST])
    r = await router.chat(None, [{"role": "user", "content": "hi"}])
    assert r.ok and r.provider == "fallback"
    assert len(primary.calls) == 1, "must not retry a non-retryable outcome"


@pytest.mark.asyncio
async def test_auth_failure_is_not_retried():
    router, primary, fallback = build(primary_script=[Outcome.AUTH_FAILED])
    r = await router.chat(None, [{"role": "user", "content": "hi"}])
    assert r.ok and r.provider == "fallback"
    assert len(primary.calls) == 1


@pytest.mark.asyncio
async def test_rate_limit_is_retried_and_penalises_the_provider():
    router, primary, _ = build(primary_script=[Outcome.RATE_LIMITED, Outcome.OK])
    r = await router.chat(None, [{"role": "user", "content": "hi"}])
    assert r.ok
    w = router.health.window("primary", "ox-1")
    assert w.total_rate_limited == 1


@pytest.mark.asyncio
async def test_circuit_opens_and_removes_route_from_candidates():
    router, primary, _ = build(
        primary_script=[Outcome.UNAVAILABLE] * 12, max_attempts=3, failure_threshold=3)
    for _ in range(2):
        await router.chat(None, [{"role": "user", "content": "hi"}])
    routes, reasons = router.candidates()
    assert all(r.provider != "primary" for r in routes)
    assert "circuit" in reasons.get("primary/ox_alpha", "")


@pytest.mark.asyncio
async def test_bad_request_does_not_trip_the_circuit():
    """A 400 is our fault or a dead model, not evidence the provider is down."""
    router, primary, _ = build(primary_script=[Outcome.BAD_REQUEST] * 10,
                               failure_threshold=2)
    for _ in range(4):
        await router.chat(None, [{"role": "user", "content": "hi"}])
    br = router.health.breaker("primary", "ox-1").to_dict()
    assert br["state"] == "closed"


@pytest.mark.asyncio
async def test_no_route_available_explains_every_exclusion():
    primary = FakeProvider("primary", spec("ox_alpha", "ox-1"))
    primary.enabled = False
    reg = Registry({"primary": primary})
    router = Router(reg, chain=[("primary", "ox_alpha")])
    with pytest.raises(NoRouteAvailable) as exc:
        await router.chat(None, [{"role": "user", "content": "hi"}])
    assert "primary/ox_alpha" in exc.value.reasons


@pytest.mark.asyncio
async def test_unavailable_model_is_excluded_after_probe():
    router, primary, _ = build()
    primary.models["ox_alpha"].available = False   # as a live probe would set
    routes, reasons = router.candidates()
    assert all(r.provider != "primary" for r in routes)
    assert "unavailable" in reasons["primary/ox_alpha"]


@pytest.mark.asyncio
async def test_tool_requirement_excludes_models_probed_without_tools():
    router, primary, _ = build()
    primary.models["ox_alpha"].supports_tools = False
    routes, reasons = router.candidates(require_tools=True)
    assert all(r.provider != "primary" for r in routes)
    assert "tool" in reasons["primary/ox_alpha"]
    # ...but it is still eligible for ordinary completions
    plain, _ = router.candidates(require_tools=False)
    assert any(r.provider == "primary" for r in plain)


@pytest.mark.asyncio
async def test_every_attempt_including_retries_takes_a_limiter_slot():
    """
    The contract counts attempts, not requests. If retries bypassed the
    limiter the NIM bound would be violated exactly when retries spike.
    """
    from oe_max.limiter import RateLimiter, VirtualClock

    clock = VirtualClock()
    primary = FakeProvider(
        "primary", spec("ox_alpha", "ox-1"),
        [Outcome.UNAVAILABLE, Outcome.UNAVAILABLE, Outcome.OK],
        limiter=RateLimiter("primary", hard_cap_per_window=44, target_rpm=42.0,
                            burst_capacity=44, clock=clock.time, sleep=clock.sleep),
    )
    reg = Registry({"primary": primary})
    router = Router(reg, chain=[("primary", "ox_alpha")],
                    retry=RetryPolicy(max_attempts=3, base_delay_s=0.0, jitter=False))
    await router.chat(None, [{"role": "user", "content": "hi"}])
    assert primary.limiter.stats.granted == 3, "each attempt must acquire a slot"


@pytest.mark.asyncio
async def test_request_log_records_provider_provenance():
    router, _, _ = build(primary_script=[Outcome.UNAVAILABLE] * 3)
    await router.chat(None, [{"role": "user", "content": "hi"}])
    assert router.request_log
    assert {"provider", "model", "outcome", "latency_ms", "attempt"} <= set(
        router.request_log[0])
    stats = router.stats_by_route()
    assert "primary/ox-1" in stats and "fallback/bk-1" in stats


@pytest.mark.asyncio
async def test_retry_policy_honours_retry_after():
    p = RetryPolicy(base_delay_s=1.0, jitter=False, honor_retry_after=True)
    assert p.delay_for(1, retry_after=7.0) == 7.0
    assert p.delay_for(3, retry_after=None) == 4.0     # 1 * 2^2
    assert p.delay_for(99, retry_after=None) == p.max_delay_s


@pytest.mark.asyncio
async def test_jitter_decorrelates_retries():
    p = RetryPolicy(base_delay_s=8.0, jitter=True)
    delays = {p.delay_for(2) for _ in range(50)}
    assert len(delays) > 5, "jitter must spread retries, not synchronise them"
    assert all(0.0 <= d <= 16.0 for d in delays)


@pytest.mark.asyncio
async def test_truncated_output_is_retried_with_a_bigger_budget():
    """
    The real failure this fixes: Ox Alpha spent 961 of 1598 completion tokens on
    hidden reasoning, truncating the diff, and 5 of 8 evolution iterations
    produced nothing from ~130-second requests. Retrying with the same budget
    reproduces the truncation exactly; the budget has to grow.
    """
    router, primary, _ = build(primary_script=[Outcome.TRUNCATED, Outcome.OK])
    r = await router.chat(None, [{"role": "user", "content": "hi"}], max_tokens=4000)
    assert r.ok
    assert len(primary.calls) == 2
    assert primary.calls[0]["params"]["max_tokens"] == 4000
    assert primary.calls[1]["params"]["max_tokens"] == 8000, "budget must escalate"


@pytest.mark.asyncio
async def test_truncation_escalation_stops_at_the_ceiling():
    """Escalating forever would burn the rate budget on ever-larger failures."""
    from oe_max.router import TOKEN_BUDGET_CEILING

    router, primary, fallback = build(
        primary_script=[Outcome.TRUNCATED] * 8, max_attempts=8)
    r = await router.chat(None, [{"role": "user", "content": "hi"}],
                          max_tokens=TOKEN_BUDGET_CEILING)
    # At the ceiling already: one attempt, then move on to the fallback route.
    assert len(primary.calls) == 1
    assert r.ok and r.provider == "fallback"


@pytest.mark.asyncio
async def test_truncation_detected_from_finish_reason():
    """A 200 carrying finish_reason=length is not a success."""
    from oe_max.providers.base import ChatResult

    res = ChatResult(Outcome.OK, "p", "m", 1.0, status_code=200, body={
        "choices": [{"finish_reason": "length",
                     "message": {"content": "half a diff"}}],
        "usage": {"total_tokens": 100,
                  "completion_tokens_details": {"reasoning_tokens": 90}},
    })
    assert res.finish_reason == "length"
    assert res.reasoning_tokens == 90
    assert res.to_log()["reasoning_tokens"] == 90


# ---------------------------------------------------------------------------
# Pinning
#
# Pinning means "this route, no failover". It must not also mean "no policy":
# a pinned reasoning model that lost budget escalation would truncate where the
# same model on the chain succeeds, and an A/B experiment between two pinned
# routes would be measuring that policy difference instead of the models.
# ---------------------------------------------------------------------------

from oe_max.router import Route


def _route(provider, model_key, model_id):
    return Route(provider=provider, model_key=model_key, model_id=model_id)


@pytest.mark.asyncio
async def test_pinned_route_never_fails_over():
    router, primary, fallback = build(primary_script=[Outcome.UNAVAILABLE] * 3)
    r = await router.chat_pinned(None, _route("primary", "ox_alpha", "ox-1"),
                                 [{"role": "user", "content": "hi"}])
    assert not r.ok and r.provider == "primary"
    assert len(fallback.calls) == 0, "pinning must not fall through to another route"


@pytest.mark.asyncio
async def test_pinned_route_still_retries():
    router, primary, fallback = build(
        primary_script=[Outcome.UNAVAILABLE, Outcome.UNAVAILABLE, Outcome.OK])
    r = await router.chat_pinned(None, _route("primary", "ox_alpha", "ox-1"),
                                 [{"role": "user", "content": "hi"}])
    assert r.ok and r.attempt == 3
    assert len(fallback.calls) == 0


@pytest.mark.asyncio
async def test_pinned_route_escalates_a_truncated_budget():
    """
    The measured case: nemotron-3-ultra-free spent 17 tokens on hidden
    reasoning against a 16-token budget and returned finish_reason=length.
    """
    router, primary, _ = build(primary_script=[Outcome.TRUNCATED, Outcome.OK])
    r = await router.chat_pinned(None, _route("primary", "ox_alpha", "ox-1"),
                                 [{"role": "user", "content": "hi"}], max_tokens=4000)
    assert r.ok
    assert [c["params"]["max_tokens"] for c in primary.calls] == [4000, 8000]


@pytest.mark.asyncio
async def test_pinned_route_records_health_like_any_other():
    """
    Otherwise a pinned experiment would leave the route's health untouched and
    the dashboard would show a model nobody had apparently called.
    """
    router, primary, _ = build(primary_script=[Outcome.OK])
    await router.chat_pinned(None, _route("primary", "ox_alpha", "ox-1"),
                             [{"role": "user", "content": "hi"}])
    assert router.health.snapshot()["primary/ox-1"]["health"]["total_attempts"] == 1
    assert router.request_log[-1]["model"] == "ox-1"


@pytest.mark.asyncio
async def test_pinning_an_unregistered_provider_is_an_error_not_a_crash():
    router, _, _ = build()
    r = await router.chat_pinned(None, _route("nope", "k", "m-1"),
                                 [{"role": "user", "content": "hi"}])
    assert not r.ok and "not registered" in (r.error or "")


@pytest.mark.asyncio
async def test_chain_behaviour_is_unchanged_by_the_refactor():
    """The chain path is the shipped default; pinning must not have moved it."""
    router, primary, fallback = build(
        primary_script=[Outcome.UNAVAILABLE] * 3, fallback_script=[Outcome.OK])
    r = await router.chat(None, [{"role": "user", "content": "hi"}])
    assert r.ok and r.provider == "fallback"
    assert len(primary.calls) == 3 and len(fallback.calls) == 1


# ---------------------------------------------------------------------------
# Demotion of a route that mostly fails
#
# Measured live: Ox Alpha at 8% success over 12 attempts, with the circuit
# breaker sitting closed on one recent failure. The reason is precise and worth
# knowing: the breaker trips on N failures inside a rolling 60-second window,
# and Ox Alpha's requests take ~300 seconds — so each failure ages out of the
# window before the next one arrives, and the breaker can never accumulate.
#
# A breaker whose window is shorter than the request latency is inert. The
# demotion below is latency-independent because its window counts attempts, not
# seconds.
# ---------------------------------------------------------------------------

def _hammer(router, provider, model, outcomes):
    from oe_max.providers.base import ChatResult

    for ok in outcomes:
        router.health.record(ChatResult(
            Outcome.OK if ok else Outcome.SERVER_ERROR, provider, model, 10.0,
            status_code=200 if ok else 500))


def test_failures_spaced_wider_than_the_window_never_trip_the_breaker():
    """
    The live cause, reproduced directly. This is not a defect in the breaker —
    it is what a *rolling time window* means — but it makes the breaker useless
    for exactly the slowest routes, where a wasted request costs the most.
    """
    from oe_max.health import CircuitBreaker

    breaker = CircuitBreaker(failure_threshold=5, window_seconds=60.0)
    now = 1_000_000.0
    for i in range(20):
        breaker.record_failure(now=now + i * 300.0)     # 300s apart, like Ox Alpha
    assert breaker.state(now + 20 * 300.0).value == "closed"
    assert breaker.trips == 0


def test_the_same_failures_close_together_do_trip_it():
    """The breaker is not broken; it is measuring something else."""
    from oe_max.health import CircuitBreaker

    breaker = CircuitBreaker(failure_threshold=5, window_seconds=60.0)
    now = 1_000_000.0
    for i in range(5):
        breaker.record_failure(now=now + i)
    assert breaker.trips == 1


def test_a_mostly_failing_route_is_demoted_out_of_the_chain():
    # A high breaker threshold so the demotion is unambiguously what excludes
    # the route, not the breaker firing first.
    router, primary, fallback = build(failure_threshold=20)
    _hammer(router, "primary", "ox-1", [False] * 11 + [True])   # 8%, like the real one

    routes, reasons = router.candidates()
    assert [str(r) for r in routes] == ["fallback/bk-1"]
    assert "degraded" in reasons["primary/ox_alpha"]
    assert "8%" in reasons["primary/ox_alpha"]


def test_a_healthy_route_is_untouched():
    router, _, _ = build(failure_threshold=20)
    _hammer(router, "primary", "ox-1", [True] * 12)
    assert router.health.degraded("primary", "ox-1") is None
    assert str(router.candidates()[0][0]) == "primary/ox-1"


def test_a_route_exactly_at_the_threshold_is_not_demoted():
    """
    The comparison is `>=`, so the stated rate is acceptable rather than
    borderline. Pinned because an off-by-one here silently changes which routes
    a run is allowed to use.
    """
    from oe_max.health import DEGRADED_SUCCESS_RATE

    router, _, _ = build(failure_threshold=20)
    _hammer(router, "primary", "ox-1", [True] * 3 + [False] * 9)   # exactly 25%
    assert abs(router.health.success_rate("primary", "ox-1")
               - DEGRADED_SUCCESS_RATE) < 1e-9
    assert router.health.degraded("primary", "ox-1") is None


def test_a_route_with_little_history_gets_the_benefit_of_the_doubt():
    """
    A fresh fallback must be able to win a route and prove itself rather than
    being locked out by two unlucky attempts.
    """
    router, _, _ = build(failure_threshold=20)
    _hammer(router, "primary", "ox-1", [False, False, False])
    assert router.health.degraded("primary", "ox-1") is None


def test_recovery_needs_no_cooldown():
    """
    The rolling window is the memory: the route comes back as soon as its
    recent attempts recover, without a timer having to expire.
    """
    router, _, _ = build(failure_threshold=20)
    _hammer(router, "primary", "ox-1", [False] * 12)
    assert router.health.degraded("primary", "ox-1")

    _hammer(router, "primary", "ox-1", [True] * 40)
    assert router.health.degraded("primary", "ox-1") is None


def test_when_everything_is_degraded_the_least_bad_still_serves():
    """
    A run that dies with "no usable route" is worse than one served slowly by a
    flaky provider. The reason says which choice was made.
    """
    router, _, _ = build(failure_threshold=20)
    _hammer(router, "primary", "ox-1", [False] * 9 + [True] * 3)     # 25%… no:
    _hammer(router, "primary", "ox-1", [False] * 3)                  # → 20%
    _hammer(router, "fallback", "bk-1", [False] * 13 + [True] * 2)   # 13%

    routes, reasons = router.candidates()
    assert len(routes) == 1
    assert str(routes[0]) == "primary/ox-1", "the least-bad route should serve"
    assert "serving anyway" in reasons["primary/ox_alpha"]
    assert "degraded" in reasons["fallback/backup"]


def test_a_tripped_breaker_is_reported_as_an_outage_not_as_degradation():
    """
    Both mechanisms exclude, and the reason has to say which — they call for
    different responses. An outage waits; a degraded route needs replacing.
    """
    router, _, _ = build()
    _hammer(router, "primary", "ox-1", [False] * 12)
    _, reasons = router.candidates()
    assert "circuit open" in reasons["primary/ox_alpha"]


@pytest.mark.asyncio
async def test_a_degraded_route_is_skipped_in_favour_of_a_healthy_one():
    router, primary, fallback = build(failure_threshold=20)
    _hammer(router, "primary", "ox-1", [False] * 11 + [True])

    r = await router.chat(None, [{"role": "user", "content": "hi"}])
    assert r.ok and r.provider == "fallback"
    assert len(primary.calls) == 0, "the degraded route was tried anyway"


@pytest.mark.asyncio
async def test_pinning_still_reaches_a_degraded_route():
    """
    Pinning means "this route" — including when it is unwell. Demotion is a
    chain-selection policy, and silently redirecting a pinned request would
    make an A/B measure something other than what it named.
    """
    router, primary, _ = build(primary_script=[Outcome.OK], failure_threshold=20)
    _hammer(router, "primary", "ox-1", [False] * 12)

    r = await router.chat_pinned(None, _route("primary", "ox_alpha", "ox-1"),
                                 [{"role": "user", "content": "hi"}])
    assert r.ok and r.provider == "primary"


# --------------------------------------------------------------------------
# An exhausted free allowance is not a rate limit
#
# Both arrive as 429. Treating them alike is wrong in both directions, so
# these tests pin the distinction from the body all the way to the chain.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_exhausted_free_pool_is_not_retried():
    """
    Retrying cannot refill a pool. The old behaviour spent the entire retry
    budget collecting the identical error before failing over.
    """
    router, primary, fallback = build(
        primary_script=[Outcome.FREE_LIMIT_EXHAUSTED] * 5, max_attempts=4)

    r = await router.chat(None, [{"role": "user", "content": "hi"}])

    assert r.ok and r.provider == "fallback"
    assert len(primary.calls) == 1, (
        f"asked an exhausted pool {len(primary.calls)} times; one is enough")


@pytest.mark.asyncio
async def test_an_exhausted_route_leaves_the_chain_entirely():
    """The second request should not rediscover what the first was told."""
    router, primary, fallback = build(
        primary_script=[Outcome.FREE_LIMIT_EXHAUSTED], max_attempts=4)

    await router.chat(None, [{"role": "user", "content": "one"}])
    calls_after_first = len(primary.calls)
    await router.chat(None, [{"role": "user", "content": "two"}])

    assert len(primary.calls) == calls_after_first, "parked route was tried again"


@pytest.mark.asyncio
async def test_exhaustion_does_not_trip_the_breaker():
    """
    The provider is up; our allowance is gone. Recording it as an outage would
    blame the provider and, worse, free the route again after a 45s cooldown
    to collect the same refusal.
    """
    router, primary, _ = build(
        primary_script=[Outcome.FREE_LIMIT_EXHAUSTED], max_attempts=4)

    await router.chat(None, [{"role": "user", "content": "hi"}])

    assert router.health.breaker("primary", "ox-1").state().value == "closed"
    assert router.health.parked("primary", "ox-1") is not None


@pytest.mark.asyncio
async def test_a_parked_route_reports_parking_not_the_circuit():
    """
    A parked route's breaker reads "closed, 0s remaining" — which says the
    route is healthy while it is being skipped. Reporting that would send an
    operator to debug the wrong subsystem.
    """
    router, _, _ = build(primary_script=[Outcome.FREE_LIMIT_EXHAUSTED], max_attempts=4)
    await router.chat(None, [{"role": "user", "content": "hi"}])

    _, reasons = router.candidates()

    assert "free allowance exhausted" in reasons["primary/ox_alpha"]
    assert "circuit" not in reasons["primary/ox_alpha"]


@pytest.mark.asyncio
async def test_an_ordinary_rate_limit_is_still_retried():
    """The guard against over-applying the new behaviour."""
    router, primary, _ = build(
        primary_script=[Outcome.RATE_LIMITED, Outcome.RATE_LIMITED, Outcome.OK],
        max_attempts=4)

    r = await router.chat(None, [{"role": "user", "content": "hi"}])

    assert r.ok and r.provider == "primary"
    assert len(primary.calls) == 3, "a plain 429 must still be retried in place"


def test_resetting_a_route_un_parks_it():
    """An operator reset means "try it now" — it has to include un-parking."""
    health = RouteHealth()
    health.park("p", "m", 900.0, "free allowance exhausted")
    assert health.allow("p", "m") is False

    health.reset("p", "m")

    assert health.allow("p", "m") is True


def test_parking_expires_on_its_own():
    health = RouteHealth()
    health.park("p", "m", 900.0, "free allowance exhausted")
    assert health.parked("p", "m", now=time.time() + 899) is not None
    assert health.parked("p", "m", now=time.time() + 901) is None


def test_the_free_limit_is_recognised_from_the_real_body():
    """
    Captured verbatim from OpenCode Zen on 2026-08-26. Note the *message* says
    "Rate limit exceeded. Please try again later." — advice that is wrong here.
    Only the error type distinguishes it, which is why the marker matches on
    the type and not on the prose.
    """
    body = ('{"type":"error","error":{"type":"FreeUsageLimitError","message":'
            '"Error from provider (Console): Rate limit exceeded. '
            'Please try again later."}}')
    assert looks_like_free_limit(body) is True


def test_a_genuine_rate_limit_body_is_not_mistaken_for_exhaustion():
    for body in ("Too Many Requests",
                 '{"error":{"message":"Rate limit reached for gpt-4","type":"requests"}}',
                 ""):
        assert looks_like_free_limit(body) is False, body


def test_exhaustion_is_not_in_the_retryable_set():
    assert Outcome.FREE_LIMIT_EXHAUSTED not in RETRYABLE


# --------------------------------------------------------------------------
# Retrying one route is bounded by wall-clock, not only by attempt count
#
# `max_attempts` is a poor bound when attempts differ in cost by two orders of
# magnitude, and on these providers they do: the primary averaged 90s per
# request on 2026-08-26 while a fallback answered a probe in 2.1s.
# --------------------------------------------------------------------------


class _Clock:
    """A monotonic clock that advances by a fixed step on every reading."""

    def __init__(self, step: float) -> None:
        self.step = step
        self.now = 0.0

    def __call__(self) -> float:
        v = self.now
        self.now += self.step
        return v


@pytest.mark.asyncio
async def test_a_slow_route_stops_being_retried_before_max_attempts(monkeypatch):
    """Four attempts at 90s is six minutes spent on a route that keeps failing."""
    router, primary, fallback = build(
        primary_script=[Outcome.SERVER_ERROR] * 8, max_attempts=4)
    router.retry.max_route_seconds = 240.0
    monkeypatch.setattr("oe_max.router.time.monotonic", _Clock(150.0))

    r = await router.chat(None, [{"role": "user", "content": "hi"}])

    assert len(primary.calls) < 4, (
        f"spent all {len(primary.calls)} attempts on a slow failing route")
    assert r.ok and r.provider == "fallback", "the chain did not fail over"


@pytest.mark.asyncio
async def test_a_fast_route_is_unaffected_by_the_time_budget(monkeypatch):
    """
    The guard that makes the budget safe to apply everywhere: when attempts are
    cheap the bound never binds, and behaviour is exactly what it was.
    """
    router, primary, _ = build(
        primary_script=[Outcome.SERVER_ERROR] * 3 + [Outcome.OK], max_attempts=4)
    router.retry.max_route_seconds = 240.0
    monkeypatch.setattr("oe_max.router.time.monotonic", _Clock(0.5))

    r = await router.chat(None, [{"role": "user", "content": "hi"}])

    assert r.ok and r.provider == "primary"
    assert len(primary.calls) == 4, "a cheap retry was cut short"


@pytest.mark.asyncio
async def test_the_recorded_reason_says_which_bound_was_hit(monkeypatch):
    """
    "gave up after 4 attempts" and "gave up after 4 minutes" point at different
    fixes. A bare failure cannot tell them apart.
    """
    router, _, _ = build(primary_script=[Outcome.SERVER_ERROR] * 8, max_attempts=4)
    router.retry.max_route_seconds = 240.0
    monkeypatch.setattr("oe_max.router.time.monotonic", _Clock(150.0))

    await router.chat(None, [{"role": "user", "content": "hi"}])

    logged = [e for e in router.request_log if e["provider"] == "primary"]
    assert any("budget" in (e.get("error") or "") for e in logged), logged


@pytest.mark.asyncio
async def test_attempt_count_still_bounds_a_fast_failing_route(monkeypatch):
    """Whichever limit binds first wins; the count has not been replaced."""
    router, primary, fallback = build(
        primary_script=[Outcome.SERVER_ERROR] * 8, max_attempts=3)
    router.retry.max_route_seconds = 10_000.0
    monkeypatch.setattr("oe_max.router.time.monotonic", _Clock(0.1))

    r = await router.chat(None, [{"role": "user", "content": "hi"}])

    assert len(primary.calls) == 3
    assert r.ok and r.provider == "fallback"


def test_the_policy_reports_no_reason_while_retrying_is_still_worthwhile():
    from oe_max.health import RetryPolicy

    policy = RetryPolicy(max_attempts=4, max_route_seconds=240.0)
    assert policy.exhausted(attempt=1, elapsed_s=5.0) is None
    assert "max_attempts" in (policy.exhausted(attempt=4, elapsed_s=5.0) or "")
    assert "budget" in (policy.exhausted(attempt=1, elapsed_s=300.0) or "")


def test_the_time_budget_can_be_switched_off():
    """Zero disables it, leaving the original attempt-count-only behaviour."""
    from oe_max.health import RetryPolicy

    policy = RetryPolicy(max_attempts=4, max_route_seconds=0.0)
    assert policy.exhausted(attempt=1, elapsed_s=99_999.0) is None
