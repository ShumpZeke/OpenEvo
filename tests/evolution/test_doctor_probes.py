"""
What the provider doctor is allowed to conclude from a response.

Every case below was a false pass, a false failure, or an undiagnosed one in the
version of the doctor that shipped before 2026-08-26. The theme is the same
throughout: HTTP 200 is not a result, and a probe that could not run is not a
failure.
"""
import asyncio
import json
import time

import pytest

from control_plane.providers.catalog import CatalogFetcher, CatalogStatus
from control_plane.providers.doctor import (
    ProbeResult, ProviderDoctor, _read_completion, apply_reports,
)
from control_plane.providers.profiles import (
    Capability, ModelProfile, Role, default_profiles,
)
from control_plane.providers.router import ModelRouter


def _profile(**kw):
    base = dict(
        id="test-model", provider="test", model="test-model",
        api_base="https://example.test/v1", secret_ref=None, requires_key=False,
        declared_capabilities=[Capability.CHAT, Capability.TOOLS],
    )
    base.update(kw)
    return ModelProfile(**base)


def _doctor(monkeypatch, responses, **kw):
    """A doctor whose HTTP layer replays `responses` (a list or a callable)."""
    d = ProviderDoctor(timeout_s=1.0, reconcile_catalog=False, **kw)
    seq = list(responses) if isinstance(responses, list) else None

    async def fake_post(url, headers, body, timeout):
        if seq is not None:
            return seq.pop(0) if len(seq) > 1 else seq[0]
        return responses(body)

    monkeypatch.setattr(d, "_post", fake_post)
    return d


def _ok(content="ok", tool_calls=None, finish="stop", reasoning=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    usage = {"completion_tokens": 5}
    if reasoning is not None:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return 200, json.dumps({
        "choices": [{"index": 0, "finish_reason": finish, "message": msg}],
        "usage": usage,
    })


_A_TOOL_CALL = [{"id": "c1", "type": "function",
                 "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'}}]


# --------------------------------------------------------------------------
# A tools probe must observe a tool call
# --------------------------------------------------------------------------

def test_a_tools_probe_needs_an_actual_tool_call():
    """
    The bug this replaces: `bool(data.get("choices"))` counted as tool support
    verified. `nemotron-3-ultra-free` answers a too-small tools request with
    HTTP 200, a null finish_reason, an empty message and no tool call, and the
    old doctor recorded TOOLS for it.
    """
    empty = json.loads(_ok(content="", finish=None)[1])
    result, detail = _read_completion(empty, with_tools=True)
    assert result is ProbeResult.FAIL
    assert "truncated" in detail or "no tool call" in detail


def test_accepting_the_tools_array_is_not_supporting_it():
    answered = json.loads(_ok(content="It is sunny in Paris.")[1])
    result, detail = _read_completion(answered, with_tools=True)
    assert result is ProbeResult.FAIL
    assert "not the same as supporting" in detail


def test_a_real_tool_call_passes_and_names_the_function():
    called = json.loads(_ok(content=None, tool_calls=_A_TOOL_CALL,
                            finish="tool_calls")[1])
    result, detail = _read_completion(called, with_tools=True)
    assert result is ProbeResult.PASS
    assert "get_weather" in detail


def test_truncation_is_diagnosed_as_truncation_not_as_a_missing_capability():
    """
    Points the operator at the token budget instead of at the model. HANDOFF
    §3.3: a reasoning model can spend 7,986 of an 8,000-token budget before its
    first visible token.
    """
    truncated = json.loads(_ok(content="", finish="length", reasoning=498)[1])
    result, detail = _read_completion(truncated, with_tools=True)
    assert result is ProbeResult.FAIL
    assert "498" in detail and "hidden reasoning" in detail


# --------------------------------------------------------------------------
# A chat probe must observe a completion
# --------------------------------------------------------------------------

def test_an_empty_completion_is_not_a_healthy_chat_route():
    empty = json.loads(_ok(content="", finish="length", reasoning=512)[1])
    result, detail = _read_completion(empty, with_tools=False)
    assert result is ProbeResult.FAIL
    assert "empty" in detail


def test_a_normal_completion_passes():
    result, _ = _read_completion(json.loads(_ok()[1]), with_tools=False)
    assert result is ProbeResult.PASS


def test_a_200_with_no_choices_fails():
    result, detail = _read_completion({"choices": []}, with_tools=False)
    assert result is ProbeResult.FAIL
    assert "no choices" in detail


# --------------------------------------------------------------------------
# Repeated attempts: an intermittent capability is not a capability
# --------------------------------------------------------------------------

def test_tools_verified_only_when_every_attempt_emits_a_call(monkeypatch):
    """
    `laguna-s-2.1-free` emitted a tool call on 1 of 3 attempts. Probed once it
    promotes itself into every agent role a third of the time.
    """
    calls = {"n": 0}

    def responder(body):
        if "tools" not in body:
            return _ok()
        calls["n"] += 1
        if calls["n"] == 1:
            return _ok(content=None, tool_calls=_A_TOOL_CALL, finish="tool_calls")
        return 503, json.dumps({"error": {"message": "Endpoint is unavailable."}})

    d = _doctor(monkeypatch, responder, tools_probe_attempts=2)
    rep = asyncio.run(d.check(_profile()))
    assert Capability.CHAT in rep.verified_capabilities
    assert Capability.TOOLS not in rep.verified_capabilities
    tools = next(p for p in rep.probes if p.name == "tools")
    assert "intermittent" in tools.detail and "1/2" in tools.detail


def test_tools_verified_when_all_attempts_succeed(monkeypatch):
    def responder(body):
        if "tools" not in body:
            return _ok()
        return _ok(content=None, tool_calls=_A_TOOL_CALL, finish="tool_calls")

    d = _doctor(monkeypatch, responder, tools_probe_attempts=2)
    rep = asyncio.run(d.check(_profile()))
    assert Capability.TOOLS in rep.verified_capabilities
    assert "2/2" in next(p for p in rep.probes if p.name == "tools").detail


def test_the_tools_prompt_actually_asks_for_the_tool(monkeypatch):
    """
    A "reply with ok" prompt carrying a `tools` array gives the model no reason
    to call anything, so a working model and a broken one answer identically.
    """
    seen = {}

    def responder(body):
        if "tools" in body:
            seen["prompt"] = body["messages"][0]["content"]
            seen["names"] = [t["function"]["name"] for t in body["tools"]]
        return _ok(content=None, tool_calls=_A_TOOL_CALL, finish="tool_calls")

    d = _doctor(monkeypatch, responder, tools_probe_attempts=1)
    asyncio.run(d.check(_profile()))
    assert "get_weather" in seen["names"]
    assert seen["names"][0] in seen["prompt"]


def test_the_probe_budget_leaves_room_for_hidden_reasoning(monkeypatch):
    budgets = []

    def responder(body):
        budgets.append(body["max_tokens"])
        return _ok(content=None, tool_calls=_A_TOOL_CALL, finish="tool_calls")

    d = _doctor(monkeypatch, responder, tools_probe_attempts=1)
    asyncio.run(d.check(_profile()))
    assert all(b >= 512 for b in budgets), budgets


# --------------------------------------------------------------------------
# A probe that could not run is not a failure
# --------------------------------------------------------------------------

def test_a_rate_limited_probe_is_inconclusive_not_a_failure(monkeypatch):
    """
    Zen answers `FreeUsageLimitError` for `mimo-v2.5-free` and `big-pickle`
    under anonymous load while both stay in its catalogue. Calling that a
    capability failure would retire a working route on someone else's traffic.
    """
    d = _doctor(monkeypatch, [(429, json.dumps(
        {"error": {"message": "Rate limit exceeded. Please try again later."}}))])
    rep = asyncio.run(d.check(_profile()))
    chat = next(p for p in rep.probes if p.name == "chat")
    assert chat.result is ProbeResult.SKIPPED
    assert "inconclusive" in chat.detail
    assert rep.summary == "rate limited; could not verify"


def test_a_cloudflare_block_stays_inconclusive(monkeypatch):
    d = _doctor(monkeypatch, [(403, "error code: 1010")])
    rep = asyncio.run(d.check(_profile()))
    chat = next(p for p in rep.probes if p.name == "chat")
    assert chat.result is ProbeResult.SKIPPED
    assert "inconclusive" in chat.detail


def test_a_route_that_only_ever_rate_limited_keeps_its_declared_capabilities(monkeypatch):
    """
    An inconclusive probe must not suppress the route: `last_probe_ok` stays
    None so the router treats it as unproven rather than broken.
    """
    d = _doctor(monkeypatch, [(429, "slow down")])
    prof = _profile()
    reps = asyncio.run(d.check_all([prof], probe_tools=True))
    apply_reports([prof], reps)
    assert prof.last_probe_ok is None
    assert prof.supports(Capability.TOOLS)   # declared, unrefuted


# --------------------------------------------------------------------------
# Catalogue reconciliation inside the doctor
# --------------------------------------------------------------------------

def _fetcher(monkeypatch_target, ids):
    f = CatalogFetcher()

    async def fake_get(url, headers, timeout):
        return 200, json.dumps({"data": [{"id": i} for i in ids]})

    monkeypatch_target.setattr(f, "_get", fake_get)
    return f


def test_a_withdrawn_model_is_named_as_withdrawn(monkeypatch):
    """
    "HTTP 401: Model x-preview-f-free is not supported" reads like an auth
    problem and sends the operator hunting for a key. The summary must lead with
    the catalogue instead.
    """
    f = _fetcher(monkeypatch, ["nemotron-3-ultra-free", "hy3-free"])
    d = ProviderDoctor(timeout_s=1.0, catalog_fetcher=f)

    async def fake_post(url, headers, body, timeout):
        return 401, json.dumps({"error": {"message": "Model x-preview-f-free is not supported"}})

    monkeypatch.setattr(d, "_post", fake_post)
    rep = asyncio.run(d.check(_profile(model="x-preview-f-free")))
    assert rep.catalog_status is CatalogStatus.ABSENT
    assert "no longer in" in rep.summary and "catalogue" in rep.summary


def test_the_catalogue_is_checked_before_the_credential_gate(monkeypatch):
    """
    Both NIM ids were absent from NIM's catalogue and this repo held no NIM key.
    Stopping at "NVIDIA_API_KEY not set" would have been true and useless: no
    key was going to make a withdrawn id serve.
    """
    f = _fetcher(monkeypatch, ["nvidia/nemotron-3-ultra-550b-a55b"])
    d = ProviderDoctor(timeout_s=1.0, catalog_fetcher=f)
    monkeypatch.delenv("SOME_KEY", raising=False)
    prof = _profile(model="deepseek-ai/deepseek-v4-pro",
                    secret_ref="SOME_KEY", requires_key=True)
    rep = asyncio.run(d.check(prof))
    assert rep.catalog_status is CatalogStatus.ABSENT
    assert "fix the model id first" in rep.summary
    assert rep.catalog_suggestions == [] or isinstance(rep.catalog_suggestions, list)


def test_a_serving_but_unlisted_model_is_flagged_not_failed(monkeypatch):
    """
    Ox Alpha served for weeks while unlisted. ABSENT must never veto a live
    probe — it annotates it.
    """
    f = _fetcher(monkeypatch, ["something-else"])
    d = ProviderDoctor(timeout_s=1.0, catalog_fetcher=f)

    async def fake_post(url, headers, body, timeout):
        if "tools" in body:
            return _ok(content=None, tool_calls=_A_TOOL_CALL, finish="tool_calls")
        return _ok()

    monkeypatch.setattr(d, "_post", fake_post)
    rep = asyncio.run(d.check(_profile(model="stealth-preview")))
    assert rep.available is True
    assert rep.catalog_status is CatalogStatus.ABSENT
    assert "unlisted" in rep.summary and "preview" in rep.summary


def test_an_unreadable_catalogue_does_not_condemn_a_route(monkeypatch):
    f = CatalogFetcher()

    async def fake_get(url, headers, timeout):
        raise ConnectionError("no route to host")

    monkeypatch.setattr(f, "_get", fake_get)
    d = ProviderDoctor(timeout_s=1.0, catalog_fetcher=f)

    async def fake_post(url, headers, body, timeout):
        return _ok()

    monkeypatch.setattr(d, "_post", fake_post)
    rep = asyncio.run(d.check(_profile()))
    assert rep.catalog_status is CatalogStatus.UNKNOWN
    assert rep.available is True
    cat = next(p for p in rep.probes if p.name == "catalog")
    assert cat.result is ProbeResult.SKIPPED


# --------------------------------------------------------------------------
# Feeding the verdict back into routing
# --------------------------------------------------------------------------

def test_a_failed_probe_takes_a_route_out_of_selection(monkeypatch):
    """
    Before this, a doctor run could establish that a route was returning HTTP
    503 and change nothing: the profile kept its declared capabilities and kept
    leading its chain, and the circuit breaker had to rediscover the outage with
    real requests.
    """
    d = _doctor(monkeypatch, [(503, json.dumps(
        {"error": {"message": "Upstream request failed: Endpoint is unavailable."}}))])
    profiles = default_profiles()
    lead = profiles[0]
    reps = asyncio.run(d.check_all([lead], probe_tools=True))
    apply_reports(profiles, reps)
    assert lead.last_probe_ok is False

    r = ModelRouter(profiles=profiles)
    _, reasons = r.candidates(Role.MUTATION)
    assert lead.id in reasons
    assert "provider doctor found this route failing" in reasons[lead.id]
    assert r.select(Role.MUTATION).id != lead.id


def test_a_stale_failed_probe_stops_suppressing_the_route():
    """
    Bounded on purpose. A provider blip at 09:00 must not keep a healthy route
    off the table all day; after the TTL it is unproven again, not condemned.
    """
    profiles = default_profiles()
    lead = profiles[0]
    lead.last_probe_ok = False
    lead.last_probe_detail = "HTTP 503"
    lead.last_probe_at = time.time() - 10_000

    r = ModelRouter(profiles=profiles, probe_ttl_s=600.0)
    assert lead.probe_is_fresh(600.0) is False
    assert r.select(Role.MUTATION).id == lead.id


def test_a_passing_probe_does_not_suppress_anything(monkeypatch):
    def responder(body):
        if "tools" in body:
            return _ok(content=None, tool_calls=_A_TOOL_CALL, finish="tool_calls")
        return _ok()

    d = _doctor(monkeypatch, responder, tools_probe_attempts=1)
    profiles = default_profiles()
    lead = profiles[0]
    reps = asyncio.run(d.check_all([lead], probe_tools=True))
    apply_reports(profiles, reps)
    assert lead.last_probe_ok is True
    assert ModelRouter(profiles=profiles).select(Role.MUTATION).id == lead.id


# --------------------------------------------------------------------------
# An unrun probe is not a result
# --------------------------------------------------------------------------

def test_skipping_the_tools_probe_does_not_erase_tool_support(monkeypatch):
    """
    Found by running `scripts/check-models.py --no-tools` and reading the
    output: every agent role reported NO ROUTE, with the reason "lacks
    capability: tools (verified by provider doctor)". Nothing had been verified
    — the probe was never run. `apply_reports` replaced the profile's
    capabilities with whatever had passed, and with tools unprobed that was
    chat alone.
    """
    d = _doctor(monkeypatch, [_ok()])
    prof = _profile()
    reps = asyncio.run(d.check_all([prof], probe_tools=False))
    apply_reports([prof], reps)

    assert reps[0].probed_capabilities == [Capability.CHAT]
    assert prof.supports(Capability.CHAT)
    assert prof.supports(Capability.TOOLS), (
        "an unprobed capability was recorded as absent"
    )


def test_skipping_the_tools_probe_leaves_every_agent_role_routable(monkeypatch):
    """The end-to-end consequence, asserted where an operator would see it."""
    def responder(body):
        return _ok()

    d = _doctor(monkeypatch, responder)
    profiles = default_profiles()
    zen = [p for p in profiles if p.provider == "opencode_zen" and p.enabled]
    reps = asyncio.run(d.check_all(zen, probe_tools=False))
    apply_reports(profiles, reps)

    r = ModelRouter(profiles=profiles)
    for role in (Role.ORCHESTRATOR, Role.DEEP_CODING, Role.REVIEW):
        chosen = r.select(role)          # raises NoRouteAvailable if broken
        r.release(chosen.id, ok=True)


def test_an_inconclusive_tools_probe_does_not_erase_tool_support(monkeypatch):
    """
    Same rule for a probe that ran but proved nothing. A 429 from a shared free
    pool says the provider was busy, not that the model cannot call tools.
    """
    def responder(body):
        if "tools" in body:
            return 429, json.dumps({"error": {"message": "Rate limit exceeded."}})
        return _ok()

    d = _doctor(monkeypatch, responder, tools_probe_attempts=2)
    prof = _profile()
    reps = asyncio.run(d.check_all([prof], probe_tools=True))
    apply_reports([prof], reps)

    assert Capability.TOOLS not in reps[0].probed_capabilities
    assert prof.supports(Capability.TOOLS)


def test_a_conclusive_tools_failure_does_erase_tool_support(monkeypatch):
    """The other direction must still work, or the merge is just a no-op."""
    def responder(body):
        if "tools" in body:
            return _ok(content="I cannot call tools.")
        return _ok()

    d = _doctor(monkeypatch, responder, tools_probe_attempts=1)
    prof = _profile()
    reps = asyncio.run(d.check_all([prof], probe_tools=True))
    apply_reports([prof], reps)

    assert Capability.TOOLS in reps[0].probed_capabilities
    assert not prof.supports(Capability.TOOLS)
    assert prof.supports(Capability.CHAT)


def test_a_recovered_capability_is_restored_by_a_later_probe(monkeypatch):
    """Self-correction has to run in both directions or it is just a blocklist."""
    prof = _profile()
    prof.verified_capabilities = [Capability.CHAT]      # as a failed probe left it

    def responder(body):
        if "tools" in body:
            return _ok(content=None, tool_calls=_A_TOOL_CALL, finish="tool_calls")
        return _ok()

    d = _doctor(monkeypatch, responder, tools_probe_attempts=1)
    reps = asyncio.run(d.check_all([prof], probe_tools=True))
    apply_reports([prof], reps)
    assert prof.supports(Capability.TOOLS)
