"""
Role chains: composition, aliasing, and the guarantees they must not lose.

Offline. The point of these is that a routing preference is a *preference* —
it must never become a filter that can starve a role, and it must never
silently name a model that does not exist.
"""

from __future__ import annotations

import pytest

from oe_max.providers.registry import Registry, build_default_registry
from oe_max.roles import (
    ALIASES, PRIMARY_ALIAS, Role, build_chains, preferences, role_for_alias,
    validate_preferences,
)
from oe_max.router import DEFAULT_CHAIN, Router


def _registry_routes():
    reg = build_default_registry()
    return [(pname, key) for pname, p in reg.items() for key in p.models]


def test_every_preferred_route_exists_in_the_registry():
    """
    A preference naming a withdrawn or renamed model degrades silently: the
    entry is skipped and the role quietly runs on its tail, which looks like a
    working system doing the wrong thing.

    This is not hypothetical. The chains in `control_plane` named
    `deepseek-ai/deepseek-v4-pro` and `qwen/qwen2.5-coder-32b-instruct` for
    months; neither is in NVIDIA's catalogue, so the "strong fallback" could
    never have served a single request.
    """
    missing = validate_preferences(_registry_routes())
    assert missing == {}, f"preferences name routes that do not exist: {missing}"


def test_no_role_can_be_starved():
    """
    Preference is an ordering, never a permission. Every role must be able to
    reach every configured route, or one provider outage turns into a dead
    role while usable capacity sits idle.
    """
    chains = build_chains(DEFAULT_CHAIN)
    for role in Role:
        assert set(DEFAULT_CHAIN) <= set(chains[role]), (
            f"{role.value} cannot reach every route")


def test_a_chain_never_repeats_a_route():
    """A duplicate would spend two attempts of the budget on one model."""
    for role, chain in build_chains(DEFAULT_CHAIN).items():
        assert len(chain) == len(set(chain)), f"{role.value} repeats a route"


def test_preferences_lead_their_chain():
    """The whole point: the preferred routes come first, in order."""
    chains = build_chains(DEFAULT_CHAIN)
    for role, preferred in preferences().items():
        assert chains[role][:len(preferred)] == preferred, role.value


def test_the_judge_and_fast_roles_lead_with_the_zero_reasoning_route():
    """
    Measured 2026-08-26 on Zen: laguna-s-2.1-free answered in 1.6s with ZERO
    reasoning tokens; nemotron-3-ultra-free took 3.3s and spent 39 on the same
    two-word prompt. End to end through the broker the gap was wider still —
    859ms against 8158ms.

    Ranking and clerical work do not need hidden reasoning, so paying for it
    buys latency and truncation risk and nothing else. If someone reorders
    these chains, this test should make them justify it.
    """
    chains = build_chains(DEFAULT_CHAIN)
    for role in (Role.JUDGE, Role.FAST):
        assert chains[role][0] == ("opencode_zen", "laguna"), role.value


def test_the_reasoning_roles_do_not_lead_with_the_fast_route():
    chains = build_chains(DEFAULT_CHAIN)
    for role in (Role.REASONER, Role.CODER):
        assert chains[role][0] == ("opencode_zen", "nemotron_ultra"), role.value


@pytest.mark.parametrize("alias,expected", sorted(
    (a, r.value) for a, r in ALIASES.items()))
def test_each_alias_selects_its_role(alias, expected):
    assert role_for_alias(alias).value == expected


def test_the_engine_alias_still_means_mutation_generation():
    """
    `oe-max-primary` is what every shipped config names. Repointing it would
    silently change what existing runs do, and every measurement already
    recorded would stop being comparable.
    """
    assert role_for_alias(PRIMARY_ALIAS) is Role.REASONER


def test_an_unknown_model_name_is_served_rather_than_refused():
    """A client that has not been told about the aliases must still work."""
    assert role_for_alias("gpt-4") is Role.REASONER
    assert role_for_alias("") is Role.REASONER


def test_a_custom_chain_produces_consistent_role_chains():
    """
    Customising `chain` must not leave the role chains pointing at the shipped
    defaults — that would give an operator a configuration whose roles ignore
    their own edit.
    """
    custom = [("opencode_zen", "hy3")]
    router = Router(Registry(build_default_registry()), chain=custom)
    for role in Role:
        assert ("opencode_zen", "hy3") in router.chains[role], role.value


def test_withdrawn_models_are_not_in_the_default_chain():
    """
    Ox Alpha stayed at the head of the chain after it stopped existing, and
    every request paid for it. Nothing believed unavailable belongs in the
    shipped ordering.
    """
    reg = build_default_registry()
    for provider_name, model_key in DEFAULT_CHAIN:
        spec = reg[provider_name].models[model_key]
        assert spec.available is not False, (
            f"{provider_name}/{spec.id} is in the default chain but is "
            f"believed unavailable: {spec.notes}")
