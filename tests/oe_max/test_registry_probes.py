"""
What a probe is allowed to conclude.

The registry's own docstring says health "must be judged over several probes
rather than one" — and the code judged on one, setting `available=False` from a
single failure. Observed live on 2026-08-26: `laguna-s-2.1-free` failed one
probe with a server error between two runs where it served normally. That one
probe would have removed the judge and fast roles' leading route from every
chain until somebody re-ran verification.

The fix is not more probes. It is noticing that most failures are not evidence
about the model at all.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest

from oe_max.providers.base import ChatResult, ModelSpec, Outcome, ProviderAdapter
from oe_max.providers.registry import Registry, _is_conclusive


class ScriptedProvider(ProviderAdapter):
    """Returns one scripted outcome for every call."""

    def __init__(self, outcome: Outcome, error: str = "", **kw):
        super().__init__("scripted", "http://fake", requires_key=False,
                         models={"m": ModelSpec(key="m", id="model-1")}, **kw)
        self.outcome = outcome
        self.error = error

    async def chat(self, client, model_id, messages, *, attempt=1, **params):
        if self.outcome is Outcome.OK:
            return ChatResult(
                Outcome.OK, self.name, model_id, 10.0, status_code=200,
                body={"choices": [{"message": {"content": "ok"},
                                   "finish_reason": "stop"}]},
            )
        return ChatResult(self.outcome, self.name, model_id, 5.0,
                          status_code=500, error=self.error)


def _verify(outcome: Outcome, error: str = "", *, believed: Optional[bool] = True):
    provider = ScriptedProvider(outcome, error)
    provider.models["m"].available = believed
    reg = Registry({"scripted": provider})
    asyncio.run(reg.verify(None, check_tools=False))
    return provider.models["m"]


@pytest.mark.parametrize("outcome", [
    Outcome.SERVER_ERROR, Outcome.UNAVAILABLE, Outcome.TIMEOUT,
    Outcome.TRANSPORT_ERROR,
])
def test_a_transient_failure_does_not_demote_a_working_route(outcome):
    """One bad minute must not cost a route its place in every chain."""
    assert _verify(outcome, "upstream had a moment").available is True


def test_the_provider_saying_the_model_is_unavailable_does_demote_it():
    """Zen's HTTP 400 for `deepseek-v4-flash-free` means what it says."""
    spec = _verify(Outcome.BAD_REQUEST, "Model is unavailable")
    assert spec.available is False


def test_an_exhausted_allowance_demotes_the_route():
    assert _verify(Outcome.FREE_LIMIT_EXHAUSTED, "FreeUsageLimitError").available is False


def test_a_withdrawal_is_recognised_despite_arriving_as_an_auth_error():
    """
    The trap that hid Ox Alpha's removal: Zen returns HTTP **401** for a
    withdrawn model, which is indistinguishable from a missing credential by
    status alone. Only the body says which.
    """
    spec = _verify(
        Outcome.AUTH_FAILED,
        '{"type":"error","error":{"type":"ModelError","message":'
        '"Model x-preview-f-free is not supported"}}')
    assert spec.available is False


def test_a_genuine_credential_failure_is_not_read_as_a_withdrawal():
    """
    The other half. Marking a model unavailable because our key is missing
    would be a false accusation against a model that works fine.
    """
    spec = _verify(
        Outcome.AUTH_FAILED,
        '{"type":"error","error":{"type":"AuthError","message":"Missing API key."}}')
    assert spec.available is True


def test_success_restores_a_route_previously_believed_dead():
    """Evidence has to work in both directions or belief only ever decays."""
    assert _verify(Outcome.OK, believed=False).available is True


def test_the_conclusiveness_rule_directly():
    assert _is_conclusive(Outcome.BAD_REQUEST, "Model is unavailable") is True
    assert _is_conclusive(Outcome.SERVER_ERROR, "502 overloaded") is False
    assert _is_conclusive(Outcome.TIMEOUT, "") is False
    assert _is_conclusive(Outcome.AUTH_FAILED, "ModelError: not supported") is True
    assert _is_conclusive(Outcome.AUTH_FAILED, "Missing API key.") is False


def test_a_transient_tools_failure_does_not_strip_a_verified_capability():
    """
    The capability filter is meant to self-correct in both directions. That
    only works if a transient failure does not also record False — otherwise
    one bad minute demotes a tools-capable model out of every agent role and
    nothing puts it back until the next verification.

    Observed live 2026-08-26: nemotron-3-ultra-free probed tools=True in one
    run and tools=False minutes later, while serving normally throughout.
    """
    calls: List[Dict[str, Any]] = []

    class FlakyToolsProvider(ProviderAdapter):
        def __init__(self):
            super().__init__("flaky", "http://fake", requires_key=False,
                             models={"m": ModelSpec(key="m", id="model-1")})

        async def chat(self, client, model_id, messages, *, attempt=1, **params):
            calls.append(params)
            if params.get("tools"):
                # A 503 on the tools call, not a rejection of tools.
                return ChatResult(Outcome.UNAVAILABLE, self.name, model_id, 5.0,
                                  status_code=503, error="temporarily overloaded")
            return ChatResult(
                Outcome.OK, self.name, model_id, 10.0, status_code=200,
                body={"choices": [{"message": {"content": "ok"},
                                   "finish_reason": "stop"}]})

    provider = FlakyToolsProvider()
    provider.models["m"].supports_tools = True
    reg = Registry({"flaky": provider})

    asyncio.run(reg.verify(None, check_tools=True))

    assert provider.models["m"].available is True
    assert provider.models["m"].supports_tools is True, (
        "a 503 on the tools probe stripped a verified capability")


def test_a_real_tools_rejection_does_strip_the_capability():
    """The self-correcting behaviour must survive the fix above."""
    class NoToolsProvider(ProviderAdapter):
        def __init__(self):
            super().__init__("notools", "http://fake", requires_key=False,
                             models={"m": ModelSpec(key="m", id="model-1")})

        async def chat(self, client, model_id, messages, *, attempt=1, **params):
            if params.get("tools"):
                return ChatResult(Outcome.BAD_REQUEST, self.name, model_id, 5.0,
                                  status_code=400, error="tools are not supported")
            return ChatResult(
                Outcome.OK, self.name, model_id, 10.0, status_code=200,
                body={"choices": [{"message": {"content": "ok"},
                                   "finish_reason": "stop"}]})

    provider = NoToolsProvider()
    provider.models["m"].supports_tools = True
    reg = Registry({"notools": provider})

    asyncio.run(reg.verify(None, check_tools=True))

    assert provider.models["m"].supports_tools is False
