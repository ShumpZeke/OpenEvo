"""The local config must disable thinking, on every path a request can take.

Without it a local reasoning model spends its whole budget on hidden reasoning
and returns an empty ``content``, which the engine reports as "No valid diffs
found in response". Measured on the tuned 27B with the same prompt:

    reasoning_effort absent : 308.7s, 978 tokens, content 0 chars,
                              reasoning 3247 chars, no diff
    reasoning_effort "none" :  99.3s, 320 tokens, content 1029 chars,
                              one applicable diff

Three times the wall clock for nothing usable. The broker's local adapter sets
it, so a run through 127.0.0.1:8787 was always fine -- but ``api_base`` is a
config field, and pointing it straight at Ollama takes the adapter out of the
path along with its setting. Both places are covered, and both are checked here.
"""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
LOCAL_CONFIG = ROOT / "configs" / "oe_max" / "local.yaml"


@pytest.fixture(scope="module")
def local_config():
    return yaml.safe_load(LOCAL_CONFIG.read_text(encoding="utf-8"))


def test_local_config_disables_reasoning(local_config):
    assert local_config["llm"]["reasoning_effort"] == "none"


def test_the_cloud_config_does_too_but_for_a_different_reason():
    """Same setting, different justification, and both worth keeping.

    Locally it is mandatory: without it the model answers entirely in
    `message.reasoning` and returns an empty `content`, so the run produces
    nothing. On NIM's flagship the reasoning is already in a separate channel
    and never ate the answer -- there it is a speed choice, measured 2026-08-29
    on a full 30-iteration run of the seeded task:

        default (on)   32m54s   best 1.4989    6 accepted improvements
        "none"         25m40s   best 1.4995   14 accepted improvements

    Asserted separately from the local one so that removing it from either
    config fails on its own terms rather than being covered by the other.
    """
    import yaml

    cloud = yaml.safe_load(
        (ROOT / "configs" / "oe_max" / "evolution.yaml").read_text(encoding="utf-8"))
    assert cloud["llm"]["reasoning_effort"] == "none"


def test_engine_propagates_it_to_the_model(local_config):
    """The engine reads it off the per-model config, not the top-level one.

    `OpenAILLM` does `getattr(model_cfg, "reasoning_effort", None)`, so a value
    that stops at `llm.reasoning_effort` and never reaches `llm.models[i]` would
    pass the check above and still send nothing.
    """
    from openevolve.config import load_config

    config = load_config(str(LOCAL_CONFIG))
    assert config.llm.reasoning_effort == "none"
    assert config.llm.models, "local config defines no models"
    for model in config.llm.models:
        assert model.reasoning_effort == "none"


def test_local_provider_adapters_still_set_it():
    """The broker path must keep working too -- this is belt and braces, and
    removing either half silently restores the failure on one route."""
    from oe_max.providers.local import build_local_providers

    adapters = build_local_providers(env={})
    assert adapters, "no local providers built"
    for name, adapter in adapters.items():
        assert getattr(adapter, "extra_body", {}).get("reasoning_effort") == "none", name


def test_operator_can_still_turn_reasoning_back_on():
    """The adapter's default must stay an override, not a hard-coding.

    A judge sometimes should think; a diff generator that thinks instead of
    answering produces nothing to apply. OE_MAX_LOCAL_REASONING is how that
    choice is made, and a test that only pinned "none" would quietly license
    removing the knob.
    """
    from oe_max.providers.local import build_local_providers

    adapters = build_local_providers(env={"OE_MAX_LOCAL_REASONING": "high"})
    for adapter in adapters.values():
        assert adapter.extra_body.get("reasoning_effort") == "high"


def test_broker_tolerates_the_field_from_the_engine():
    """With it set in the config the engine now sends `reasoning_effort` in the
    request body, so the broker's request model has to accept it rather than
    reject the request as malformed."""
    from oe_max.broker.app import ChatCompletionRequest

    request = ChatCompletionRequest(
        messages=[{"role": "user", "content": "hi"}],
        reasoning_effort="none",
    )
    assert request.messages[0].content == "hi"
