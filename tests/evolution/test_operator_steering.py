"""
Steering each mutation with a named operator class.

Upstream issues one undifferentiated "improve this program" request, so every
mutation is the same mutation and no credit assignment is possible —
`route_quality.by_operator` had nowhere to get an operator from and was empty
by construction.

Two things here are load-bearing rather than cosmetic:

* it must be off unless asked for, because it changes what the model is asked
  and would confound every comparison already recorded;
* it must never touch the evaluator's sampler. Telling an evaluator to
  "substitute a fundamentally different algorithm" corrupts the score rather
  than the candidate, which is far harder to notice than a broken diff.
"""

import types

import pytest

from control_plane.telemetry import instrument as inst


class FakeSampler:
    """Stands in for upstream's PromptSampler."""

    system_template_override = None
    user_template_override = None

    def build_prompt(self, **kw):
        return {"system": "You are an expert algorithm designer.",
                "user": "CURRENT PROGRAM:\n```python\nx = 1\n```"}


@pytest.fixture
def steering(monkeypatch):
    """Install the hook onto a stand-in sampler class, enabled."""
    monkeypatch.setenv(inst.ENV_OPERATORS, "1")
    monkeypatch.setattr(inst, "_worker_operator", None, raising=False)

    module = types.ModuleType("openevolve.prompt.sampler")
    module.PromptSampler = FakeSampler
    monkeypatch.setitem(__import__("sys").modules,
                        "openevolve.prompt.sampler", module)
    original = FakeSampler.build_prompt
    inst.install_operator_hook()
    yield FakeSampler
    FakeSampler.build_prompt = original


def test_it_is_off_unless_asked_for(monkeypatch):
    monkeypatch.delenv(inst.ENV_OPERATORS, raising=False)
    assert inst.operators_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE"])
def test_the_usual_truthy_spellings_all_enable_it(monkeypatch, value):
    monkeypatch.setenv(inst.ENV_OPERATORS, value)
    assert inst.operators_enabled() is True


def test_the_directive_goes_into_the_system_message(steering):
    """
    Not the user message: that carries the exact SEARCH/REPLACE format
    contract, and a model that reads the directive as part of the diff spec
    costs the whole request.
    """
    prompt = FakeSampler().build_prompt(evolution_round=3)
    assert "MUTATION TYPE:" in prompt["system"]
    assert "MUTATION TYPE:" not in prompt["user"]
    assert prompt["user"] == "CURRENT PROGRAM:\n```python\nx = 1\n```"


def test_the_original_system_message_is_kept(steering):
    prompt = FakeSampler().build_prompt(evolution_round=1)
    assert prompt["system"].startswith("You are an expert algorithm designer.")


def test_the_evaluator_sampler_is_never_steered(steering):
    """
    Upstream marks it with set_templates("evaluator_system_message"). Steering
    it would corrupt the score rather than the candidate.
    """
    sampler = FakeSampler()
    sampler.system_template_override = "evaluator_system_message"
    prompt = sampler.build_prompt(evolution_round=3)
    assert "MUTATION TYPE:" not in prompt["system"]


def test_the_choice_is_reproducible_for_a_given_iteration(steering):
    """
    Workers are separate processes with no shared state, so the seed is the
    only thing keeping a rerun comparable to the run it is compared against.
    """
    a = inst._select_operator("run_1", 7, has_failure=False, has_second_parent=False)
    b = inst._select_operator("run_1", 7, has_failure=False, has_second_parent=False)
    assert a is not None and a == b


def test_different_iterations_get_different_operators(steering):
    picks = {inst._select_operator("run_1", i, has_failure=False,
                                   has_second_parent=False) for i in range(30)}
    assert len(picks) > 1, "every iteration got the same operator"


def test_operators_needing_context_they_lack_are_not_offered():
    """
    Asking for COUNTEREXAMPLE_REPAIR with no counterexample produces a vague
    request, and the bad numbers get blamed on a good operator.
    """
    from oe_max.search.operators import OPERATORS, OperatorClass

    picks = {inst._select_operator("run_1", i, has_failure=False,
                                   has_second_parent=False) for i in range(200)}
    for name in picks:
        op = OPERATORS[OperatorClass(name)]
        assert not op.needs_failure and not op.needs_second_parent


def test_context_unlocks_the_operators_that_need_it():
    picks = {inst._select_operator("run_1", i, has_failure=True,
                                   has_second_parent=True) for i in range(200)}
    assert "COUNTEREXAMPLE_REPAIR" in picks or "ADVERSARIAL_REPAIR" in picks
    assert "CROSS_LINEAGE_RECOMBINATION" in picks


def test_the_chosen_operator_is_attached_to_the_request(steering):
    """Steering without labelling would buy the prompt and lose the analysis."""
    FakeSampler().build_prompt(evolution_round=5)
    inst._begin_worker_attribution()
    inst._worker_operator = "STRUCTURAL_REWRITE"
    inst._publish_generation({"request_id": "req_1", "provider": "opencode_zen",
                              "model": "hy3-free", "at": 0.0})

    assert inst._take_worker_attribution()["operator"] == "STRUCTURAL_REWRITE"


def test_a_prompt_is_never_lost_to_a_steering_failure(steering, monkeypatch):
    """The mutation matters; the label does not. A failure must not cost both."""
    monkeypatch.setattr(inst, "_select_operator",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    prompt = FakeSampler().build_prompt(evolution_round=1)
    assert prompt["user"] and prompt["system"]


def test_installing_twice_does_not_double_append(steering):
    inst.install_operator_hook()
    prompt = FakeSampler().build_prompt(evolution_round=2)
    assert prompt["system"].count("MUTATION TYPE:") == 1


# ---------------------------------------------------------------------------
# Island policies
#
# The island a candidate's parent came from is in the snapshot the worker is
# handed and never reaches `build_prompt` — so without capturing it in the
# worker wrapper, an island policy could not influence the one thing it exists
# to influence.
# ---------------------------------------------------------------------------

def _snapshot(islands=4, parent_island=1, parent_id="p1"):
    return {
        "islands": [set() for _ in range(islands)],
        "current_island": 0,
        "programs": {parent_id: {"id": parent_id, "code": "x = 1",
                                 "metadata": {"island": parent_island}}},
        "artifacts": {},
    }


@pytest.fixture
def worker_island(monkeypatch):
    monkeypatch.setattr(inst, "_worker_island", None, raising=False)
    monkeypatch.setattr(inst, "_worker_island_count", 0, raising=False)
    yield
    inst._worker_island = None
    inst._worker_island_count = 0


def test_the_island_is_captured_from_the_snapshot(worker_island):
    inst._record_worker_island((_snapshot(islands=4, parent_island=2), "p1", []), {})
    assert inst._worker_island == 2
    assert inst._worker_island_count == 4


def test_a_parent_with_no_island_falls_back_to_the_sampling_island(worker_island):
    snap = _snapshot()
    snap["programs"]["p1"]["metadata"] = {}
    snap["current_island"] = 3
    inst._record_worker_island((snap, "p1", []), {})
    assert inst._worker_island == 3


def test_a_malformed_snapshot_disables_policies_rather_than_raising(worker_island):
    inst._record_worker_island(("not a snapshot",), {})
    assert inst._worker_island_count == 0


def test_policies_are_off_unless_asked_for(monkeypatch):
    monkeypatch.delenv(inst.ENV_ISLAND_POLICIES, raising=False)
    assert inst.island_policies_enabled() is False


def test_selection_follows_the_island_policy(monkeypatch, worker_island):
    """
    Island 0 is `exploit` and island 1 is `explore`, so over many iterations
    the same code path must produce measurably different mutation classes.
    """
    import statistics

    from oe_max.search.operators import OPERATORS, OperatorClass

    monkeypatch.setenv(inst.ENV_ISLAND_POLICIES, "1")
    inst._worker_island_count = 4

    def mean_disruption(island):
        inst._worker_island = island
        picks = [inst._select_operator("run_1", i, has_failure=False,
                                       has_second_parent=False)
                 for i in range(400)]
        return statistics.mean(
            OPERATORS[OperatorClass(p)].disruption for p in picks if p)

    assert mean_disruption(0) < mean_disruption(1), \
        "exploit island should ask for smaller changes than the explore island"


def test_selection_is_unbiased_when_policies_are_off(monkeypatch, worker_island):
    import statistics

    from oe_max.search.operators import OPERATORS, OperatorClass, applicable

    monkeypatch.delenv(inst.ENV_ISLAND_POLICIES, raising=False)
    inst._worker_island_count, inst._worker_island = 4, 0
    picks = [inst._select_operator("run_1", i, has_failure=False,
                                   has_second_parent=False) for i in range(600)]
    observed = statistics.mean(
        OPERATORS[OperatorClass(p)].disruption for p in picks if p)
    unweighted = statistics.mean(
        OPERATORS[c].disruption for c in applicable())
    assert abs(observed - unweighted) < 0.06


def test_the_policy_is_recorded_with_the_request(monkeypatch, worker_island):
    """Steering an island without labelling it would buy the search and lose
    the ability to tell whether it helped."""
    monkeypatch.setenv(inst.ENV_ISLAND_POLICIES, "1")
    inst._worker_island_count, inst._worker_island = 4, 1

    inst._begin_worker_attribution()
    inst._worker_island_count, inst._worker_island = 4, 1
    inst._publish_generation({"request_id": "req_1", "provider": "p",
                              "model": "m", "at": 0.0})

    assert inst._take_worker_attribution()["island_policy"] == "explore"
