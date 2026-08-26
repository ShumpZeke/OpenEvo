"""
Heterogeneous island policies.

Upstream's islands are structurally separate and behaviourally identical: every
island runs the same search, so migration exchanges programs between
populations that were exploring the same way. A policy per island makes
migration mean something specific — the explorer supplies raw material the
exploiter would never propose, the exploiter supplies a refined baseline the
explorer can wreck productively.

The mechanism is `Operator.disruption`, declared on all 15 operators since the
taxonomy was written and read by nothing until now.
"""

import random
import statistics

import pytest

from oe_max.search.operators import OPERATORS, applicable
from oe_max.search.policies import (
    BALANCED, DEFAULT_ROTATION, EXPLOIT, EXPLORE, MIN_WEIGHT, REFINE, assign,
    choose, describe, policy_for,
)

CANDIDATES = applicable()
UNWEIGHTED = statistics.mean(OPERATORS[c].disruption for c in CANDIDATES)


def _mean_disruption(policy, trials=3000, seed=0):
    rng = random.Random(seed)
    picks = [choose(policy, CANDIDATES, rng) for _ in range(trials)]
    return statistics.mean(OPERATORS[p].disruption for p in picks)


# -- the bias is real -------------------------------------------------------

def test_explore_and_exploit_land_either_side_of_the_baseline():
    """
    The point of naming them differently. If both sat near the unweighted mean
    the policy layer would be decoration.
    """
    exploit, explore = _mean_disruption(EXPLOIT), _mean_disruption(EXPLORE)
    assert exploit < UNWEIGHTED < explore
    assert explore - exploit > 0.3, "the islands barely separate"


def test_balanced_reproduces_the_unweighted_taxonomy():
    """The check that the weighting introduces no bias of its own."""
    assert abs(_mean_disruption(BALANCED) - UNWEIGHTED) < 0.02


def test_refine_sits_between_exploit_and_balanced():
    assert _mean_disruption(EXPLOIT) < _mean_disruption(REFINE) < _mean_disruption(BALANCED)


def test_each_policy_prefers_the_operators_it_is_named_for():
    rng = random.Random(0)
    explore = [choose(EXPLORE, CANDIDATES, rng) for _ in range(2000)]
    rng = random.Random(0)
    exploit = [choose(EXPLOIT, CANDIDATES, rng) for _ in range(2000)]

    assert explore.count(OPERATORS["RADICAL_RETHINK"].cls) > \
        exploit.count(OPERATORS["RADICAL_RETHINK"].cls)
    assert exploit.count(OPERATORS["PARAMETER_CHANGE"].cls) > \
        explore.count(OPERATORS["PARAMETER_CHANGE"].cls)


# -- a bias, never a ban ----------------------------------------------------

def test_no_operator_is_unreachable_on_any_island():
    """
    An explorer that can never tune a parameter cannot turn a structural idea
    into a working program. A wrong bias should cost efficiency, not shut a
    search direction off entirely.
    """
    for policy in (EXPLORE, EXPLOIT, BALANCED, REFINE):
        for op in CANDIDATES:
            assert policy.weight_for(op) >= MIN_WEIGHT > 0


def test_every_operator_is_actually_drawn_given_enough_trials():
    rng = random.Random(0)
    drawn = {choose(EXPLOIT, CANDIDATES, rng) for _ in range(4000)}
    assert drawn == set(CANDIDATES)


def test_sampling_is_weighted_not_argmax():
    """
    Always taking the single best-fitting operator would collapse an island to
    one mutation class — a worse search than the uniform one it replaced.
    """
    rng = random.Random(0)
    picks = {choose(EXPLORE, CANDIDATES, rng) for _ in range(200)}
    assert len(picks) > 3


# -- assignment -------------------------------------------------------------

def test_islands_get_different_policies():
    assigned = assign(4)
    assert len({p.name for p in assigned}) == 4


def test_assignment_is_stable_across_runs():
    """
    An experiment cannot attribute a difference to the policy layer if the
    layer itself differs run to run.
    """
    assert [p.name for p in assign(6)] == [p.name for p in assign(6)]


def test_the_first_island_is_conservative():
    """
    Upstream seeds and samples island 0 most. Spending its whole budget on
    rewrites would make a single-island run strictly worse than no policies.
    """
    assert assign(1)[0] is EXPLOIT
    assert DEFAULT_ROTATION[0] is EXPLOIT


def test_more_islands_than_policies_wraps_round_robin():
    names = [p.name for p in assign(9)]
    assert names[:4] == names[4:8]
    assert len(names) == 9


def test_an_unknown_island_falls_back_to_balanced():
    """Not knowing where a candidate came from is not a reason to bias it."""
    assert policy_for(None, 4) is BALANCED
    assert policy_for(0, 0) is BALANCED


def test_zero_islands_assigns_nothing():
    assert assign(0) == []


def test_describe_is_reportable():
    rows = describe(3)
    assert [r["island_id"] for r in rows] == [0, 1, 2]
    assert all(r["policy"] and r["description"] for r in rows)
