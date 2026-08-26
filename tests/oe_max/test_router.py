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
from typing import Any, Dict, List, Optional

import pytest

from oe_max.health import RetryPolicy, RouteHealth
from oe_max.providers.base import (
    ChatResult, ModelSpec, Outcome, ProviderAdapter, ProviderRole,
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
                  Outcome.SERVER_ERROR: 500}.get(outcome, 500)
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
