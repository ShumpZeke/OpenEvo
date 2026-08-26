"""
The ablation arms' environments.

A hardcoded path in an arm degrades silently when `--task` changes: seeding
skips entirely and verification quietly falls back to generic checks only — an
arm that looks like it ran and tested almost nothing. That is worse than a
crash, because the run produces a number.
"""

import importlib.util
import os

import pytest

SPEC = importlib.util.spec_from_file_location("ablation", "scripts/ablation.py")
ablation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ablation)


def test_every_arm_names_the_question_it_answers():
    """An arm whose question nobody wrote down produces a number nobody can read."""
    for name, arm in ablation.ARMS.items():
        assert arm["asks"].endswith("?"), name
        assert arm["env"], name


def test_the_evaluator_path_follows_the_task():
    env = ablation.arm_env("seed_forge", "circle_packing")
    assert env["EVOLUTION_EVALUATOR_PATH"] == os.path.join(
        "examples", "circle_packing", "evaluator.py")


def test_arms_that_do_not_need_the_evaluator_do_not_get_it():
    """Extra environment is extra difference between arms."""
    assert "EVOLUTION_EVALUATOR_PATH" not in ablation.arm_env(
        "operators", "function_minimization")


def test_verification_is_pointed_at_the_evolved_function():
    """
    function_minimization evolves `search_algorithm`; verifying the default
    `run_search` would check a wrapper rather than the thing that changed.
    """
    env = ablation.arm_env("verify", "function_minimization")
    assert env["OE_MAX_VERIFY_ENTRY_POINT"] == "search_algorithm"


def test_the_island_policy_arm_turns_operator_steering_on_too():
    """Policies act through operator selection; without it the arm is a no-op."""
    env = ablation.arm_env("island_policies", "function_minimization")
    assert env["OE_MAX_OPERATORS"] == "1"
    assert env["OE_MAX_ISLAND_POLICIES"] == "1"


def test_arms_that_need_comparing_against_another_arm_say_so():
    """
    The island-policy arm shares its environment with `operators`, so comparing
    it only against the baseline attributes the whole difference to the policy
    layer.
    """
    assert "operators" in ablation.ARMS["island_policies"]["note"]


def test_building_an_arm_does_not_mutate_the_registry():
    """Otherwise the second repeat runs with the first repeat's accumulated env."""
    before = dict(ablation.ARMS["seed_forge"]["env"])
    ablation.arm_env("seed_forge", "function_minimization")
    assert ablation.ARMS["seed_forge"]["env"] == before


def test_the_baseline_is_the_absence_of_every_arm():
    """
    Which is also what a plain upstream run does — the comparison is only
    meaningful if the control is genuinely uncontrolled.
    """
    for arm in ablation.ARMS.values():
        assert arm["env"], "an arm with no environment is not an arm"
