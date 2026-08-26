"""
The shipped configs must route each kind of request to the right role.

These are cheap tests guarding an expensive class of mistake: a config that
loads without error and quietly sends every request to one model. Nothing
fails, nothing warns, and the only symptom is a bill or a latency figure
nobody connects to the cause.
"""

from __future__ import annotations

import os

import pytest

from openevolve.config import load_config

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MAX_CONFIG = os.path.join(ROOT, "configs", "oe_max", "evolution.yaml")
STOCK_CONFIG = os.path.join(ROOT, "configs", "oe_max", "stock_baseline.yaml")


def test_mutation_and_judging_go_to_different_routes():
    """
    Upstream already separates these (`evaluator_models` defaults to a copy of
    `models`), so pointing them at different aliases costs no engine change.

    Measured 2026-08-26: the judge alias resolves to laguna-s-2.1-free, which
    reports ZERO hidden-reasoning tokens, where the reasoner alias spent 39 on
    a two-word answer. Truncation in the judge is also the expensive kind of
    failure — it corrupts the *score* rather than the candidate, which is much
    harder to notice than a broken diff.
    """
    cfg = load_config(MAX_CONFIG)

    assert [m.name for m in cfg.llm.models] == ["oe-max-primary"]
    assert [m.name for m in cfg.llm.evaluator_models] == ["oe-max-judge"]


def test_llm_judging_stays_off_by_default():
    """
    Wired is not the same as switched on. Enabling LLM feedback changes what
    evolution optimises, so every measurement already recorded would stop being
    comparable. That has to be a deliberate choice, not a default that arrived
    with a routing change.
    """
    assert load_config(MAX_CONFIG).evaluator.use_llm_feedback is False


def test_every_configured_model_is_a_known_broker_alias():
    """
    A config naming an alias the broker does not know still works — unknown
    names fall through to the reasoner — which is exactly why a typo here would
    never surface. `oe-max-judge` misspelt is silently the reasoner, and the
    only evidence is a latency figure.
    """
    from oe_max.roles import ALIASES

    cfg = load_config(MAX_CONFIG)
    for model in list(cfg.llm.models) + list(cfg.llm.evaluator_models):
        assert model.name in ALIASES, (
            f"{model.name!r} is not a broker alias; it would silently route to "
            f"the default role. Known: {sorted(ALIASES)}")


def test_both_shipped_configs_point_somewhere_real():
    """
    The stock baseline talks to the provider directly, with no broker and so no
    failover: a withdrawn model there does not degrade the arm, it makes every
    request fail. It named `x-preview-f-free` until that model was withdrawn.
    """
    stock = load_config(STOCK_CONFIG)
    names = [m.name for m in stock.llm.models]
    assert names, "the baseline arm names no model at all"

    from oe_max.providers.registry import build_default_registry

    zen = build_default_registry()["opencode_zen"]
    serveable = {s.id for s in zen.models.values() if s.available is not False}
    for name in names:
        assert name in serveable, (
            f"the baseline arm names {name!r}, which is not a Zen route "
            f"believed serveable. Known good: {sorted(serveable)}")


@pytest.mark.parametrize("path", [MAX_CONFIG, STOCK_CONFIG])
def test_configs_load(path):
    assert load_config(path) is not None
