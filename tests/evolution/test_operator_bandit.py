"""
Wiring the bandit to real reward.

The bandit's two halves live in different processes: selection in a worker,
reward in the main process. These tests cover the main-process half and the
gates, because the failure modes here are all silent — a bandit that learns
from the wrong population, or double-counts, still runs and still returns an
operator.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from control_plane.telemetry import instrument as I
from oe_max.search.operators import OperatorClass

# Real operators from the taxonomy: the store drops evidence for arms that
# do not exist, which is correct and makes invented names untestable.
GOOD = OperatorClass.LOCAL_OPTIMIZE.value
BAD = OperatorClass.STRUCTURAL_REWRITE.value


class _Cfg:
    feature_dimensions = ["complexity", "diversity"]


def _program(pid, *, parent_id=None, score=None, operator=GOOD,
             migrant=False):
    md = {}
    if operator:
        md[I.ATTRIBUTION_KEY] = {"request_id": "r1", "provider": "p",
                                 "model": "m", "operator": operator}
    if migrant:
        md["migrant"] = True
    return SimpleNamespace(
        id=pid, parent_id=parent_id, metadata=md,
        metrics={"combined_score": score} if score is not None else {},
    )


def _db(programs):
    return SimpleNamespace(config=_Cfg(), programs={p.id: p for p in programs})


@pytest.fixture
def bandit_on(monkeypatch, tmp_path):
    monkeypatch.setenv(I.ENV_OPERATORS, "1")
    monkeypatch.setenv(I.ENV_OPERATOR_BANDIT, "1")
    monkeypatch.setenv("EVOLUTION_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(I, "_active", SimpleNamespace(run_id="run_1"))
    return I._bandit_store("run_1")


# -- gates -----------------------------------------------------------------


def test_the_bandit_is_off_by_default(monkeypatch):
    monkeypatch.delenv(I.ENV_OPERATOR_BANDIT, raising=False)
    monkeypatch.setenv(I.ENV_OPERATORS, "1")
    assert I.operator_bandit_enabled() is False


def test_the_bandit_requires_operator_steering(monkeypatch):
    """
    Without a named operator there is nothing to attribute reward to, so
    enabling this alone would be a flag that silently does nothing.
    """
    monkeypatch.delenv(I.ENV_OPERATORS, raising=False)
    monkeypatch.setenv(I.ENV_OPERATOR_BANDIT, "1")
    assert I.operator_bandit_enabled() is False


def test_no_reward_is_recorded_while_the_bandit_is_off(monkeypatch, tmp_path):
    monkeypatch.delenv(I.ENV_OPERATOR_BANDIT, raising=False)
    monkeypatch.setenv(I.ENV_OPERATORS, "1")
    monkeypatch.setenv("EVOLUTION_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(I, "_active", SimpleNamespace(run_id="run_1"))

    child = _program("c", parent_id="p", score=1.4)
    I._reward_operator(_db([_program("p", score=1.0), child]),
                       child, accepted=True, fitness=1.4)

    assert I._bandit_store("run_1").load().snapshot()["total_pulls"] == 0


# -- reward semantics ------------------------------------------------------


def test_an_improvement_rewards_its_operator(bandit_on):
    child = _program("c", parent_id="p", score=1.4, operator=GOOD)
    I._reward_operator(_db([_program("p", score=1.0), child]),
                       child, accepted=True, fitness=1.4)

    arms = bandit_on.load().snapshot()["arms"]
    assert arms[GOOD]["pulls"] == 1
    assert arms[GOOD]["last_reward"] > 0.5, "an improvement scored no better than a regression"


def test_a_regression_is_rewarded_less_than_an_improvement(bandit_on):
    worse = _program("w", parent_id="p", score=0.5, operator=GOOD)
    I._reward_operator(_db([_program("p", score=1.0), worse]),
                       worse, accepted=True, fitness=0.5)
    regression = bandit_on.load().snapshot()["arms"][GOOD]["last_reward"]

    bandit_on.reset()
    better = _program("b", parent_id="p", score=1.4, operator=GOOD)
    I._reward_operator(_db([_program("p", score=1.0), better]),
                       better, accepted=True, fitness=1.4)
    improvement = bandit_on.load().snapshot()["arms"][GOOD]["last_reward"]

    assert improvement > regression


def test_a_rejected_candidate_is_recorded_as_a_real_outcome(bandit_on):
    """
    Recording only accepted candidates would teach the bandit that an operator
    producing nothing but duplicates is indistinguishable from one never tried.
    """
    rejected = _program("r", parent_id="p", operator=GOOD)
    I._reward_operator(_db([_program("p", score=1.0)]), rejected,
                       accepted=False, fitness=None)

    arms = bandit_on.load().snapshot()["arms"]
    assert arms[GOOD]["pulls"] == 1
    assert arms[GOOD]["last_reward"] == 0.0


def test_a_migrant_is_not_rewarded_again(bandit_on):
    """
    `_migrate_programs` copies metadata wholesale, so a migrant carries the
    operator of the mutation that made the *original*. Rewarding it again
    counts one mutation once per island it reaches — the same trap that made
    two analysis modules measure the wrong population.
    """
    migrant = _program("m", parent_id="p", score=1.4, operator=GOOD,
                       migrant=True)
    I._reward_operator(_db([_program("p", score=1.0), migrant]),
                       migrant, accepted=True, fitness=1.4)

    assert bandit_on.load().snapshot()["total_pulls"] == 0


def test_an_unattributed_candidate_is_not_credited_to_anything(bandit_on):
    """The seed program has no operator; inventing one would be a fabrication."""
    seed = _program("s", operator=None, score=1.0)
    I._reward_operator(_db([seed]), seed, accepted=True, fitness=1.0)

    assert bandit_on.load().snapshot()["total_pulls"] == 0


def test_a_candidate_whose_parent_is_gone_is_still_credited(bandit_on):
    """
    A parent can be evicted before its child is added. Skipping the reward
    would quietly drop evidence for exactly the long-lineage candidates that
    matter most.
    """
    child = _program("c", parent_id="vanished", score=1.4, operator=GOOD)
    I._reward_operator(_db([child]), child, accepted=True, fitness=1.4)

    assert bandit_on.load().snapshot()["arms"][GOOD]["pulls"] == 1


def test_learning_never_raises_into_the_evolution_loop(bandit_on, monkeypatch):
    """A broken bandit must cost exploitation, never a candidate."""
    def _boom(*a, **kw):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(I, "_bandit_store", _boom)
    child = _program("c", parent_id="p", score=1.4)

    I._reward_operator(_db([_program("p", score=1.0), child]),
                       child, accepted=True, fitness=1.4)   # must not raise


# -- the loop actually closes ----------------------------------------------


def test_reward_in_the_main_process_changes_what_a_worker_would_select(bandit_on):
    """
    The end-to-end claim, with the process boundary stood in for by separate
    store objects: rewards recorded by `_reward_operator` must reach the
    selection path, or the bandit is still not in the loop.
    """
    db = _db([_program("p", score=1.0)])
    for i in range(30):
        good = _program(f"g{i}", parent_id="p", score=1.6, operator=GOOD)
        db.programs[good.id] = good
        I._reward_operator(db, good, accepted=True, fitness=1.6)
        bad = _program(f"b{i}", parent_id="p", operator=BAD)
        I._reward_operator(db, bad, accepted=False, fitness=None)

    picks = [I._bandit_store("run_1").select([GOOD, BAD])
             for _ in range(30)]

    assert picks.count(GOOD) >= 25, (
        f"the worker-side selector ignored main-process reward "
        f"({picks.count(GOOD)}/30)")
