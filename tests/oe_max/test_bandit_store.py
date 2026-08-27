"""
Bandit state crossing the process boundary.

The bandit was built, tested and unused for one structural reason: selection
happens in a worker and reward is known in the main process, and they share no
memory. HANDOFF §3.7 covers worker→main (`Program.metadata`); this is the other
direction, which had no channel at all.

These tests treat a fresh `BanditStore` object as a stand-in for a different
process, because that is exactly what it is: no shared state, only the file.
"""

from __future__ import annotations

import json
import os

import pytest

from oe_max.search.bandit import build_selector, reward_from_outcome
from oe_max.search.bandit_store import BanditStore, deserialise, serialise

ARMS = ["rewrite", "tighten", "vectorise"]


@pytest.fixture
def store(tmp_path):
    return BanditStore(str(tmp_path / "bandit.json"), ARMS)


def _fresh(store):
    """A new object over the same file — what another process sees."""
    return BanditStore(store.path, ARMS)


def test_evidence_written_by_one_object_is_visible_to_another(store):
    """The whole point. Without this the bandit cannot exist at all."""
    store.update("tighten", 1.0)

    assert _fresh(store).load().stats["tighten"].pulls == 1


def test_the_bandit_converges_on_the_arm_that_pays(store):
    """Learning has to survive the round trip, not merely the write."""
    for _ in range(40):
        _fresh(store).update("tighten", reward_from_outcome(
            accepted=True, fitness_delta=0.4))
        _fresh(store).update("rewrite", reward_from_outcome(accepted=False))
        _fresh(store).update("vectorise", reward_from_outcome(accepted=False))

    picks = [_fresh(store).select() for _ in range(40)]

    assert picks.count("tighten") >= 35, (
        f"chose the paying arm only {picks.count('tighten')}/40 times")


def test_no_state_file_is_not_an_error(store):
    """A missing history means "no evidence", not "refuse to run"."""
    assert not os.path.exists(store.path)
    assert store.select() in ARMS
    assert store.load().snapshot()["total_pulls"] == 0


def test_a_corrupt_state_file_falls_back_rather_than_raising(store, tmp_path):
    """
    A half-written or hand-edited file must cost some exploitation, never the
    mutation. The run matters more than the bandit.
    """
    with open(store.path, "w", encoding="utf-8") as fh:
        fh.write("{not json at all")

    assert store.select() in ARMS
    assert store.update("tighten", 1.0) is True


@pytest.mark.parametrize("junk", ["[]", '"a string"', "null", "42"])
def test_state_that_is_valid_json_but_the_wrong_shape_is_ignored(store, junk):
    with open(store.path, "w", encoding="utf-8") as fh:
        fh.write(junk)

    assert store.select() in ARMS


def test_a_reader_never_sees_a_half_written_file(store):
    """
    Atomic replace is what makes single-writer/many-reader safe. A reader gets
    the previous state or the next one, never a truncated one.
    """
    store.update("tighten", 1.0)
    for _ in range(20):
        store.update("rewrite", 0.5)
        with open(store.path, "r", encoding="utf-8") as fh:
            json.load(fh)      # raises if a partial write were ever visible


def test_no_temporary_files_are_left_behind(store, tmp_path):
    for _ in range(5):
        store.update("tighten", 1.0)

    leftovers = [n for n in os.listdir(tmp_path) if n.startswith(".bandit-")]
    assert leftovers == [], leftovers


def test_evidence_for_an_arm_that_no_longer_exists_is_dropped():
    """
    When the operator taxonomy changes, a posterior for a removed operator
    would otherwise persist forever — and `select()` could return an arm the
    caller cannot act on.
    """
    selector = build_selector("discounted_thompson", ARMS + ["retired_operator"])
    selector.update("retired_operator", 1.0)

    rebuilt = deserialise(serialise(selector), ARMS)

    assert "retired_operator" not in rebuilt.stats
    assert set(rebuilt.stats) == set(ARMS)


def test_one_malformed_arm_does_not_discard_the_others():
    state = {"selector": "discounted_thompson", "arms": {
        "tighten": {"alpha": 9.0, "beta": 1.0, "pulls": 8, "total_reward": 8.0},
        "rewrite": {"alpha": "not a number"},
    }}

    rebuilt = deserialise(state, ARMS)

    assert rebuilt.stats["tighten"].pulls == 8
    assert rebuilt.stats["rewrite"].pulls == 0


def test_an_unknown_selector_name_falls_back_rather_than_failing():
    """A state file from a future version must not break an older one."""
    state = {"selector": "some_future_selector", "arms": {}}

    assert deserialise(state, ARMS) is not None


def test_reset_clears_the_history(store):
    store.update("tighten", 1.0)
    store.reset()

    assert store.load().snapshot()["total_pulls"] == 0
    store.reset()          # idempotent: a missing file is not an error


def test_snapshot_reports_unreadable_state_rather_than_inventing_one(tmp_path):
    store = BanditStore(str(tmp_path / "nope" / "bandit.json"), ARMS)

    snap = store.snapshot()

    assert snap["total_pulls"] == 0
    assert snap["path"].endswith("bandit.json")
