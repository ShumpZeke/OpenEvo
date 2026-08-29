"""Single-model mode: one model answers everything, and the operator picks it.

The mode exists so that "is this model any good here?" and "A against B" are
answerable at all — a run where the chain quietly served three other models
cannot answer either.

Which makes its most important property a negative one: **it must never
silently fall back to a chain.** A run that reports one model while three
answered is worse than a run that stops, so an unsatisfiable selection is a 503
and not a shrug. Most of what follows is about that.

No network. The provider is scripted and records what it was asked.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from oe_max import single_model
from oe_max.broker.app import create_app
from oe_max.providers.base import ChatResult, ModelSpec, Outcome, ProviderAdapter
from oe_max.providers.registry import Registry


class Scripted(ProviderAdapter):
    def __init__(self, name: str, models: Dict[str, ModelSpec]):
        super().__init__(name, "http://fake", requires_key=False, models=models)
        self.calls: List[str] = []

    async def chat(self, client, model_id, messages, *, attempt=1, **params):
        self.calls.append(model_id)
        return ChatResult(
            Outcome.OK, self.name, model_id, 12.0, status_code=200,
            body={"choices": [{"message": {"role": "assistant",
                                           "content": f"served by {model_id}"},
                               "finish_reason": "stop"}],
                  "usage": {"total_tokens": 7}},
            attempt=attempt)


MODELS = {
    "nemotron_ultra": ModelSpec(key="nemotron_ultra", id="nemotron-3-ultra-free"),
    "laguna": ModelSpec(key="laguna", id="laguna-s-2.1-free"),
    "hy3": ModelSpec(key="hy3", id="hy3-free"),
}


@pytest.fixture(autouse=True)
def _clean_mode():
    """The mode is process state; leaking it between tests would be its own bug."""
    single_model.reset_for_tests()
    yield
    single_model.reset_for_tests()


@contextmanager
def _client(models: Optional[Dict[str, ModelSpec]] = None):
    provider = Scripted("opencode_zen", dict(models or MODELS))
    with TestClient(create_app(Registry({"opencode_zen": provider}))) as client:
        yield client, provider


def _ask(client, model="oe-max-primary"):
    return client.post("/v1/chat/completions", json={
        "model": model, "messages": [{"role": "user", "content": "hi"}]})


# -- off by default ---------------------------------------------------------


def test_the_mode_is_off_until_asked_for():
    with _client() as (client, provider):
        body = client.get("/v1/oe-max/single-model").json()
        assert body["enabled"] is False
        assert body["selected"] is None
        assert body["reason"] == "off"

        assert _ask(client).status_code == 200
        # Normal role routing: the reasoner chain's head, not some single model.
        assert provider.calls == ["nemotron-3-ultra-free"]


def test_the_picker_lists_what_can_be_chosen():
    with _client() as (client, _provider):
        body = client.get("/v1/oe-max/single-model").json()
        ids = {c["model_id"] for c in body["candidates"]}
        assert ids == {"nemotron-3-ultra-free", "laguna-s-2.1-free", "hy3-free"}
        for candidate in body["candidates"]:
            assert candidate["label"].startswith("opencode_zen/")


# -- picking ----------------------------------------------------------------


def test_every_role_goes_to_the_one_model():
    """The whole point. Four aliases that normally reach two different models."""
    with _client() as (client, provider):
        assert client.post("/v1/oe-max/single-model",
                           json={"model": "laguna"}).status_code == 200

        for alias in ("oe-max-primary", "oe-max-reasoner",
                      "oe-max-coder", "oe-max-judge", "oe-max-fast"):
            assert _ask(client, alias).status_code == 200

        assert set(provider.calls) == {"laguna-s-2.1-free"}, provider.calls
        assert len(provider.calls) == 5


def test_it_overrides_a_per_request_pin():
    """A caller naming a concrete model must not escape the mode.

    Otherwise "only this model answers" is a preference the client can argue
    with, and the mode cannot be trusted to mean what it says.
    """
    with _client() as (client, provider):
        client.post("/v1/oe-max/single-model", json={"model": "laguna"})
        assert _ask(client, "hy3-free").status_code == 200
        assert provider.calls == ["laguna-s-2.1-free"]


def test_a_substring_is_enough():
    with _client() as (client, provider):
        r = client.post("/v1/oe-max/single-model", json={"model": "hy3"})
        assert r.status_code == 200
        assert r.json()["route"]["model_id"] == "hy3-free"
        _ask(client)
        assert provider.calls == ["hy3-free"]


def test_clearing_returns_to_role_chains():
    with _client() as (client, provider):
        client.post("/v1/oe-max/single-model", json={"model": "laguna"})
        _ask(client)
        client.post("/v1/oe-max/single-model", json={"model": None})

        body = client.get("/v1/oe-max/single-model").json()
        assert body["enabled"] is False

        _ask(client, "oe-max-reasoner")
        assert provider.calls[-1] == "nemotron-3-ultra-free"


# -- refusing rather than guessing ------------------------------------------


def test_an_unknown_model_is_refused_at_the_point_it_is_typed():
    """Not at the next request, which could be minutes later and elsewhere."""
    with _client() as (client, _provider):
        r = client.post("/v1/oe-max/single-model", json={"model": "gpt-9"})
        assert r.status_code == 409
        assert "gpt-9" in str(r.json())
        # And the mode stayed off rather than half-on.
        assert client.get("/v1/oe-max/single-model").json()["enabled"] is False


def test_an_ambiguous_query_names_what_it_matched():
    """Picking one silently is how you run a model you did not choose."""
    with _client() as (client, _provider):
        r = client.post("/v1/oe-max/single-model", json={"model": "free"})
        assert r.status_code == 409
        detail = str(r.json())
        assert "matches 3 models" in detail, detail
        assert "hy3-free" in detail


def test_an_exact_id_wins_over_a_longer_substring_match():
    """`laguna-s-2.1-free` is a substring of nothing else, but `free` matches
    three — an exact hit must never be reported as ambiguous."""
    with _client() as (client, _provider):
        r = client.post("/v1/oe-max/single-model",
                        json={"model": "laguna-s-2.1-free"})
        assert r.status_code == 200
        assert r.json()["route"]["model_id"] == "laguna-s-2.1-free"


# -- the property that matters ----------------------------------------------


def test_an_unsatisfiable_selection_fails_instead_of_falling_back():
    """The mode's reason for existing, stated as a test.

    A model selected and then withdrawn by discovery must stop the run, not
    quietly hand the work to a chain. A run that reports one model while three
    answered is the failure this mode exists to prevent.
    """
    with _client() as (client, provider):
        client.post("/v1/oe-max/single-model", json={"model": "laguna"})
        assert _ask(client).status_code == 200

        # Discovery withdraws it out from under the mode.
        del provider.models["laguna"]
        provider.calls.clear()

        r = _ask(client)
        assert r.status_code == 503, r.status_code
        assert not provider.calls, "it served from a chain instead of failing"
        detail = str(r.json())
        assert "single-model" in detail and "laguna" in detail


def test_the_status_endpoint_reports_the_mode():
    """An operator reading status must be able to tell which mode a run was in;
    otherwise a recorded result cannot be interpreted later."""
    with _client() as (client, _provider):
        assert client.get("/v1/oe-max/status").json()["single_model"]["enabled"] is False
        client.post("/v1/oe-max/single-model", json={"model": "hy3"})
        reported = client.get("/v1/oe-max/status").json()["single_model"]
        assert reported["enabled"] is True
        assert reported["route"]["model_id"] == "hy3-free"
        assert reported["ok"] is True


def test_a_broken_selection_is_visible_in_status_rather_than_silent():
    with _client() as (client, provider):
        client.post("/v1/oe-max/single-model", json={"model": "laguna"})
        del provider.models["laguna"]

        reported = client.get("/v1/oe-max/status").json()["single_model"]
        assert reported["enabled"] is True
        assert reported["ok"] is False
        assert reported["route"] is None
        assert "laguna" in reported["reason"]


def test_a_disabled_model_cannot_be_selected():
    """The mode must not report `ok` for a route the router will skip.

    `Router` filters `spec.available is False` before attempting anything, so
    selecting one gives a mode that says it is fine while every request 503s --
    the "says one thing, does another" failure this mode exists to prevent,
    pointed at itself. Found by asking whether the picker could offer the 183s
    deepseek that was deliberately disabled.

    The refusal carries the recorded reason, because "why can I not pick this"
    is the next question.
    """
    from oe_max.providers.base import ModelSpec
    from oe_max.providers.registry import Registry

    models = {
        "good": ModelSpec(key="good", id="good-model"),
        "slow": ModelSpec(key="slow", id="slow-model", available=False,
                          notes="183.5s on a one-word prompt; disabled by choice"),
    }
    registry = Registry({"opencode_zen": Scripted("opencode_zen", models)})

    route, reason = single_model.resolve(registry, "slow-model")
    assert route is None
    assert "disabled" in reason
    assert "183.5s" in reason, "the recorded reason was dropped"

    # And the usable one still resolves, so this is not just refusing everything.
    route, reason = single_model.resolve(registry, "good-model")
    assert route is not None and reason == "ok"


def test_an_unprobed_model_is_still_selectable():
    """`available is None` means nobody has probed it, which is the normal state
    for most models and must not be read as `False`."""
    from oe_max.providers.base import ModelSpec
    from oe_max.providers.registry import Registry

    models = {"fresh": ModelSpec(key="fresh", id="fresh-model")}
    registry = Registry({"opencode_zen": Scripted("opencode_zen", models)})

    assert getattr(models["fresh"], "available", "missing") is None
    route, reason = single_model.resolve(registry, "fresh-model")
    assert route is not None and reason == "ok"


def test_the_endpoint_refuses_a_disabled_model_with_its_reason():
    """End to end: the operator sees why, at the moment they click it."""
    from oe_max.providers.base import ModelSpec

    models = dict(MODELS)
    models["broken"] = ModelSpec(key="broken", id="broken-model", available=False,
                                 notes="probe returned 404 for this account")
    with _client(models) as (client, _provider):
        r = client.post("/v1/oe-max/single-model", json={"model": "broken-model"})
        assert r.status_code == 409
        detail = str(r.json())
        assert "disabled" in detail
        assert "404" in detail
