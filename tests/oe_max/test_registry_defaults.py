"""
The shipped broker registry and DEFAULT_CHAIN must reference models that exist.

`test_router.py` exercises routing against synthetic `FakeProvider`s, which is
the right way to test the *logic* and the reason the shipped defaults went
unchecked: on 2026-08-26 `DEFAULT_CHAIN` began with two withdrawn model ids —
`x-preview-f-free` on Zen and `stealth/ox-alpha` on OpenRouter — so every
request through the broker started with two guaranteed failures, and 250 broker
tests passed throughout.

These tests check the shipped configuration itself. They are deliberately
offline: a network assertion would make the suite flaky and slow, and the live
check belongs in the provider doctor. What they can catch is the whole class of
bug where the table refers to something that is not there.
"""
import pytest

from oe_max.providers.registry import build_default_registry
from oe_max.router import DEFAULT_CHAIN


# Model ids observed to be dead on 2026-08-26. Not an exhaustive blocklist —
# it cannot be — but a regression guard: re-adding one of these should require
# deleting the line that says it does not work.
WITHDRAWN_IDS = {
    "x-preview-f-free":      "withdrawn from OpenCode Zen; HTTP 401 'not supported'",
    "stealth/ox-alpha":      "not in OpenRouter's catalogue",
    "ox-alpha-free":         "alias of the withdrawn x-preview-f-free",
    "deepseek-ai/deepseek-v4-pro":       "absent from NVIDIA NIM's catalogue",
    "qwen/qwen2.5-coder-32b-instruct":   "absent from NVIDIA NIM's catalogue",
    "deepseek-v4-flash-free":            "listed by Zen, answers 'Model is unavailable'",
    "muse-spark-1.2-contributor-free":   "HTTP 500 on every attempt",
}


@pytest.fixture
def registry():
    return build_default_registry()


def test_every_chain_entry_resolves_to_a_configured_provider(registry):
    for provider_name, model_key in DEFAULT_CHAIN:
        assert provider_name in registry, (
            f"DEFAULT_CHAIN references unknown provider {provider_name!r}"
        )


def test_every_chain_entry_resolves_to_a_configured_model(registry):
    """
    The specific failure: a chain entry naming a model key the provider no
    longer configures is skipped at runtime with "model not configured for this
    provider" — quietly, on every single request.
    """
    for provider_name, model_key in DEFAULT_CHAIN:
        provider = registry[provider_name]
        assert model_key in provider.models, (
            f"DEFAULT_CHAIN references {provider_name}/{model_key}, which is "
            f"not configured. Available: {sorted(provider.models)}"
        )


def test_no_chain_entry_uses_a_known_withdrawn_model_id(registry):
    for provider_name, model_key in DEFAULT_CHAIN:
        spec = registry[provider_name].models[model_key]
        assert spec.id not in WITHDRAWN_IDS, (
            f"DEFAULT_CHAIN routes through {spec.id!r} — {WITHDRAWN_IDS[spec.id]}"
        )


def test_no_configured_model_anywhere_uses_a_known_withdrawn_id(registry):
    """Also covers models configured but not currently chained."""
    for provider_name, provider in registry.items():
        for key, spec in provider.models.items():
            assert spec.id not in WITHDRAWN_IDS, (
                f"{provider_name}/{key} is {spec.id!r} — {WITHDRAWN_IDS[spec.id]}"
            )


def test_the_chain_leads_with_a_provider_that_needs_no_credential(registry):
    """
    The broker must do something useful out of the box. Zen's free tier serves
    without an Authorization header, and if the chain led with a key-requiring
    provider a fresh checkout would fail every request before reaching it.
    """
    provider = registry[DEFAULT_CHAIN[0][0]]
    assert provider.requires_key is False, (
        f"DEFAULT_CHAIN leads with {provider.name}, which requires "
        f"{provider.api_key_env}"
    )


def test_every_configured_model_has_a_non_empty_wire_id(registry):
    for provider_name, provider in registry.items():
        for key, spec in provider.models.items():
            assert spec.id and spec.id.strip(), f"{provider_name}/{key} has no id"
            assert spec.id == spec.id.strip(), f"{provider_name}/{key} id has whitespace"


def test_chain_entries_are_unique(registry):
    assert len(DEFAULT_CHAIN) == len(set(DEFAULT_CHAIN)), (
        "a duplicated chain entry retries the same route twice and reports it "
        "as two independent failures"
    )


def test_nim_configures_no_models_statically(registry):
    """
    NIM ids must be discovered live, not remembered. Both ids that were once
    written here from memory turned out never to have existed on NIM.
    """
    assert registry["nvidia_nim"].models == {}, (
        "NIM model ids must come from live discovery — see the registry docstring"
    )
