"""
Operator taxonomy and adaptive selection.

The bandit tests focus on the property that motivates the choice: tracking a
*change* in which operator is best. A stationary bandit passes "finds the best
arm" and still fails the job, because what is best changes during a run.
"""
import math
import pytest

from oe_max.search.bandit import (
    DiscountedThompsonSampling, EpsilonGreedy, SELECTORS, UniformRandom,
    build_selector, reward_from_outcome,
)
from oe_max.search.operators import (
    OPERATORS, OperatorClass, applicable, build_prompt,
)


# ------------------------------------------------------------- operators
def test_every_operator_class_has_a_definition():
    for cls in OperatorClass:
        assert cls in OPERATORS, f"{cls} has no Operator entry"
        assert OPERATORS[cls].instruction.strip()


def test_context_free_operators_are_always_applicable():
    ops = applicable()
    assert OperatorClass.LOCAL_OPTIMIZE in ops
    assert OperatorClass.RADICAL_RETHINK in ops


def test_operators_needing_context_are_filtered_out_without_it():
    """
    Asking for COUNTEREXAMPLE_REPAIR with no counterexample yields a vague
    request — and the bandit would then learn a good operator is useless.
    """
    ops = applicable(has_failure=False, has_second_parent=False)
    assert OperatorClass.COUNTEREXAMPLE_REPAIR not in ops
    assert OperatorClass.CROSS_LINEAGE_RECOMBINATION not in ops

    ops = applicable(has_failure=True, has_second_parent=True)
    assert OperatorClass.COUNTEREXAMPLE_REPAIR in ops
    assert OperatorClass.CROSS_LINEAGE_RECOMBINATION in ops


def test_exclusion_list_respected():
    ops = applicable(exclude=[OperatorClass.RADICAL_RETHINK])
    assert OperatorClass.RADICAL_RETHINK not in ops


def test_prompt_contains_operator_and_diff_format():
    p = build_prompt(OperatorClass.ALGORITHM_SUBSTITUTION, "def f(): pass")
    assert "ALGORITHM_SUBSTITUTION" in p
    assert "<<<<<<< SEARCH" in p and ">>>>>>> REPLACE" in p
    assert "def f(): pass" in p


def test_crossover_prompt_includes_both_parents():
    p = build_prompt(OperatorClass.CROSS_LINEAGE_RECOMBINATION, "AAA",
                     second_parent="BBB")
    assert "AAA" in p and "BBB" in p


def test_repair_prompt_includes_the_failure():
    p = build_prompt(OperatorClass.COUNTEREXAMPLE_REPAIR, "AAA",
                     failure_context="fails on n=0")
    assert "fails on n=0" in p


# --------------------------------------------------------------- bandit
def test_selector_registry_is_complete():
    for name in ("discounted_thompson", "epsilon_greedy", "uniform_random"):
        assert name in SELECTORS
        assert build_selector(name, ["a", "b"]) is not None
    with pytest.raises(ValueError):
        build_selector("nope", ["a"])


def test_thompson_converges_on_the_better_arm():
    s = DiscountedThompsonSampling(["good", "bad"], gamma=0.99, seed=1)
    for _ in range(300):
        arm = s.select()
        s.update(arm, 0.9 if arm == "good" else 0.05)
    assert s.stats["good"].mean > s.stats["bad"].mean
    picks = [s.select() for _ in range(200)]
    assert picks.count("good") > picks.count("bad") * 2


def test_discounting_lets_the_selector_change_its_mind():
    """
    The non-stationary property. Arm A pays for the first phase, then stops and
    B starts paying. A stationary bandit keeps choosing A on accumulated
    evidence; a discounted one must switch.
    """
    s = DiscountedThompsonSampling(["a", "b"], gamma=0.85, seed=7)
    for _ in range(150):                       # phase 1: a is good
        s.update("a", 0.95)
        s.update("b", 0.05)
    assert s.stats["a"].mean > s.stats["b"].mean

    for _ in range(150):                       # phase 2: the world flips
        s.update("a", 0.05)
        s.update("b", 0.95)
    assert s.stats["b"].mean > s.stats["a"].mean, "failed to track the change"


def test_gamma_one_is_stationary_and_does_not_forget():
    s = DiscountedThompsonSampling(["a"], gamma=1.0)
    assert math.isinf(s.evidence_half_life)


def test_evidence_half_life_is_reported():
    s = DiscountedThompsonSampling(["a"], gamma=0.95)
    assert 12 < s.evidence_half_life < 15


def test_invalid_gamma_rejected():
    with pytest.raises(ValueError):
        DiscountedThompsonSampling(["a"], gamma=0.0)
    with pytest.raises(ValueError):
        DiscountedThompsonSampling(["a"], gamma=1.5)


def test_selection_can_be_restricted_to_applicable_arms():
    s = DiscountedThompsonSampling(list(OperatorClass), seed=3)
    allowed = [OperatorClass.LOCAL_OPTIMIZE, OperatorClass.PARAMETER_CHANGE]
    for _ in range(30):
        assert s.select(allowed) in allowed


def test_rewards_are_clamped():
    s = DiscountedThompsonSampling(["a"])
    s.update("a", 5.0)
    s.update("a", -3.0)
    assert 0.0 <= s.stats["a"].mean <= 1.0
    assert s.stats["a"].last_reward == 0.0


def test_unknown_arm_is_registered_on_update():
    s = DiscountedThompsonSampling(["a"])
    s.update("new", 1.0)
    assert "new" in s.stats and s.stats["new"].pulls == 1


def test_empty_candidate_pool_raises():
    s = DiscountedThompsonSampling(["a"])
    with pytest.raises(ValueError):
        s.select(["not-an-arm"])


def test_uniform_random_is_a_real_ablation():
    """The 'no bandit' arm must still record stats so ablations are comparable."""
    s = UniformRandom(["a", "b", "c"], seed=5)
    for _ in range(150):
        s.update(s.select(), 1.0 if s.arms[0] == "a" else 0.0)
    counts = [s.stats[a].pulls for a in ("a", "b", "c")]
    assert all(c > 20 for c in counts), f"not uniform: {counts}"


def test_epsilon_greedy_explores_and_exploits():
    s = EpsilonGreedy(["good", "bad"], epsilon=0.1, seed=2)
    for _ in range(200):
        arm = s.select()
        s.update(arm, 0.9 if arm == "good" else 0.1)
    assert s.stats["good"].pulls > s.stats["bad"].pulls
    assert s.stats["bad"].pulls > 0, "must keep exploring"


# --------------------------------------------------------------- reward
def test_rejected_candidate_scores_zero():
    assert reward_from_outcome(accepted=False) == 0.0
    assert reward_from_outcome(accepted=False, fitness_delta=10.0) == 0.0


def test_accepted_without_improvement_scores_small_but_positive():
    """Widening the archive has value in a quality-diversity search."""
    r = reward_from_outcome(accepted=True, fitness_delta=0.0)
    assert 0.0 < r < 0.5


def test_improvement_scores_higher_and_saturates():
    small = reward_from_outcome(accepted=True, fitness_delta=0.01)
    big = reward_from_outcome(accepted=True, fitness_delta=0.5)
    huge = reward_from_outcome(accepted=True, fitness_delta=500.0)
    assert small < big <= huge <= 1.0
    assert huge - big < 0.2, "a single outlier must not dominate the posterior"


def test_snapshot_is_serialisable_and_ranked():
    import json
    s = DiscountedThompsonSampling(list(OperatorClass), seed=1)
    for _ in range(20):
        s.update(s.select(), 0.5)
    snap = s.snapshot()
    json.dumps(snap)
    assert snap["selector"] == "discounted_thompson"
    assert len(snap["ranking"]) == len(OperatorClass)
