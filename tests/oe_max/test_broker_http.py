"""
The broker's HTTP surface, tested offline against scripted providers.

The broker is the single point every model request passes through, and until
now none of its HTTP behaviour was tested at all — only the router underneath
it. The two are not the same thing: alias resolution, the provenance stamp,
which upstream status is passed through and which is masked, and the shape of a
total failure all live in the handler.

No network. Each test scripts the upstream and asserts what a client sees.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from oe_max.broker.app import create_app
from oe_max.providers.base import ChatResult, ModelSpec, Outcome, ProviderAdapter
from oe_max.providers.registry import Registry


class Scripted(ProviderAdapter):
    """Replays outcomes per model id, and records what it was asked."""

    def __init__(self, name: str, models: Dict[str, ModelSpec],
                 script: Optional[Dict[str, List[Outcome]]] = None):
        super().__init__(name, "http://fake", requires_key=False, models=models)
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.calls: List[str] = []

    async def chat(self, client, model_id, messages, *, attempt=1, **params):
        self.calls.append(model_id)
        queue = self.script.get(model_id)
        outcome = queue.pop(0) if queue else Outcome.OK
        if outcome is Outcome.OK:
            return ChatResult(
                Outcome.OK, self.name, model_id, 12.0, status_code=200,
                body={"choices": [{"message": {"role": "assistant",
                                               "content": f"served by {model_id}"},
                                   "finish_reason": "stop"}],
                      "usage": {"total_tokens": 7}},
                attempt=attempt)
        status = {Outcome.RATE_LIMITED: 429, Outcome.AUTH_FAILED: 401,
                  Outcome.BAD_REQUEST: 400,
                  Outcome.FREE_LIMIT_EXHAUSTED: 429}.get(outcome, 500)
        return ChatResult(outcome, self.name, model_id, 4.0, status_code=status,
                          error=f"scripted {outcome.value}", attempt=attempt)


@contextmanager
def _client(script=None, models=None):
    """
    A started broker plus the provider behind it.

    Entering the TestClient matters: the broker builds its shared httpx client
    on startup and every handler refuses with 503 until it exists, so a client
    used without the context manager tests nothing but that guard.
    """
    models = models or {
        "nemotron_ultra": ModelSpec(key="nemotron_ultra", id="nemotron-3-ultra-free"),
        "laguna": ModelSpec(key="laguna", id="laguna-s-2.1-free"),
        "hy3": ModelSpec(key="hy3", id="hy3-free"),
    }
    provider = Scripted("opencode_zen", models, script)
    app = create_app(Registry({"opencode_zen": provider}))
    with TestClient(app) as client:
        yield client, provider


def _ask(client, model, **kw):
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
    body.update(kw)
    return client.post("/v1/chat/completions", json=body)


# -- aliases ---------------------------------------------------------------


@pytest.mark.parametrize("alias,expected", [
    ("oe-max-primary", "nemotron-3-ultra-free"),
    ("oe-max-reasoner", "nemotron-3-ultra-free"),
    ("oe-max-coder", "nemotron-3-ultra-free"),
    ("oe-max-judge", "laguna-s-2.1-free"),
    ("oe-max-fast", "laguna-s-2.1-free"),
])
def test_each_alias_reaches_its_role_s_route(alias, expected):
    with _client() as (client, provider):
        r = _ask(client, alias)
        assert r.status_code == 200
        assert provider.calls[0] == expected, f"{alias} went to {provider.calls[0]}"


def test_naming_a_concrete_model_pins_it():
    """Pinning is how an A/B arm names what it is measuring."""
    with _client() as (client, provider):
        r = _ask(client, "hy3-free")
        assert r.status_code == 200
        assert provider.calls == ["hy3-free"]
        assert r.json()["oe_max"]["role"] == "pinned"


def test_an_unknown_model_is_served_rather_than_refused():
    """
    A client that has not been told about the aliases must still work — the
    engine's default config named a bare model long before roles existed.
    """
    with _client() as (client, _):
        assert _ask(client, "gpt-4-turbo").status_code == 200


# -- provenance ------------------------------------------------------------


def test_every_response_says_which_route_actually_served_it():
    """
    Through the broker every request names an alias, so without this stamp all
    traffic collapses into one row called `oe-max-primary` and no per-route
    analysis is possible at all. See HANDOFF 3.8.
    """
    with _client() as (client, _):
        stamp = _ask(client, "oe-max-judge").json()["oe_max"]

    assert stamp["provider"] == "opencode_zen"
    assert stamp["model"] == "laguna-s-2.1-free"
    assert stamp["role"] == "judge"
    assert stamp["outcome"] == "ok"


def test_the_stamp_survives_failover_and_names_the_route_that_worked():
    """The route that served is not the route that was asked for."""
    script = {"nemotron-3-ultra-free": [Outcome.SERVER_ERROR] * 6}
    with _client(script) as (client, _):
        stamp = _ask(client, "oe-max-reasoner").json()["oe_max"]

    assert stamp["model"] != "nemotron-3-ultra-free"
    assert stamp["outcome"] == "ok"


# -- failure shapes --------------------------------------------------------


def test_a_total_failure_explains_every_excluded_route():
    """
    An operator whose run has stopped needs to know why each route was passed
    over. "No usable route" alone sends them looking in the wrong place.
    """
    models = {"only": ModelSpec(key="only", id="only-model", available=False)}
    with _client(models=models) as (client, _):
        r = _ask(client, "oe-max-primary")

    assert r.status_code == 503
    excluded = r.json()["detail"]["excluded"]
    assert excluded, "a 503 with no reasons is not actionable"
    assert all(v for v in excluded.values())


def test_an_exhausted_allowance_is_reported_as_itself_not_as_a_rate_limit():
    script = {mid: [Outcome.FREE_LIMIT_EXHAUSTED] * 4
              for mid in ("nemotron-3-ultra-free", "laguna-s-2.1-free", "hy3-free")}
    with _client(script) as (client, _):
        r = _ask(client, "oe-max-primary")

    assert r.status_code == 429
    assert r.json()["error"]["type"] == "free_limit_exhausted"


def test_streaming_is_refused_honestly_rather_than_faked():
    """Pretending to stream would fail subtly, inside a client's parser."""
    with _client() as (client, _):
        r = _ask(client, "oe-max-primary", stream=True)

    assert r.status_code == 400
    assert "stream" in str(r.json()).lower()


def test_unknown_request_fields_are_accepted_rather_than_rejected():
    """
    OpenEvolve and other OpenAI clients send extra keys. 422-ing a request we
    could have served would break the engine for no benefit.
    """
    with _client() as (client, _):
        r = _ask(client, "oe-max-primary", frequency_penalty=0.2,
                 user="someone", logit_bias={})
    assert r.status_code == 200


# -- discovery surface -----------------------------------------------------


def test_models_advertises_the_aliases_before_the_concrete_ids():
    """
    A client that picks the first entry of /v1/models should get a routed
    chain with failover, not a single pinned provider.
    """
    with _client() as (client, _):
        data = client.get("/v1/models").json()["data"]

    assert data[0]["id"].startswith("oe-max-")
    assert data[0]["oe_max"]["alias_for_role"]
    assert any(m["id"] == "laguna-s-2.1-free" for m in data)


def test_health_reports_provider_usability():
    with _client() as (client, _):
        body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["providers"]["opencode_zen"]["usable"] is True


# -- startup ---------------------------------------------------------------


def test_startup_verification_does_not_hold_up_the_socket():
    """
    Verification smoke-tests every model on every usable provider, and each
    probe is a real completion. Measured with the shipped catalogue: ~65
    seconds before the broker accepted a connection.

    Meanwhile `run-evolution.sh` gives up on /health after 5 seconds and tells
    the operator the broker is not running — wrong, and the most confusing
    possible message given they just started it. So verification runs in the
    background and the broker serves immediately.
    """
    slow = Scripted("opencode_zen", {
        "m": ModelSpec(key="m", id="slow-model"),
    })
    probed: List[str] = []

    async def _slow_chat(client, model_id, messages, *, attempt=1, **params):
        import asyncio
        probed.append(model_id)
        await asyncio.sleep(30)      # never completes within the test
        raise AssertionError("unreachable")

    slow.chat = _slow_chat          # type: ignore[assignment]
    app = create_app(Registry({"opencode_zen": slow}), verify_on_start=True)

    with TestClient(app) as client:
        # If verification were awaited in the lifespan, entering the context
        # manager above would block for 30s and this would never run.
        body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["verified_at"] is None, (
        "nothing may claim to be verified before the probe has finished")


def test_a_failing_startup_probe_does_not_stop_the_broker_serving():
    """
    `/health` reporting `verified_at: null` is the honest state — we have not
    verified, rather than we verified and found nothing.
    """
    broken = Scripted("opencode_zen", {"m": ModelSpec(key="m", id="m-1")})

    async def _boom(*a, **kw):
        raise RuntimeError("provider listing exploded")

    broken.list_models = _boom      # type: ignore[assignment]
    broken.chat = _boom             # type: ignore[assignment]
    app = create_app(Registry({"opencode_zen": broken}), verify_on_start=True)

    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"
