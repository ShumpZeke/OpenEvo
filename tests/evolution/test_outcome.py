"""
Did the run get anywhere?

Route quality says which route produced better mutations; throughput says how
many candidates per request. Neither answers the question an ablation asks:
did this configuration find a better program for the same number of requests?

The two measurement choices here are the whole content: best-so-far against
*requests* rather than wall-clock, and area under that curve rather than the
final score.
"""

import pytest

from control_plane.analysis.outcome import (
    MIN_REQUESTS_PER_ARM, area_under, best_so_far, compare, measure,
)
from control_plane.storage.store import Store
from control_plane.telemetry.events import Component, Event, EventType


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "cp.db"))
    yield s
    s.close()


def _request(store, run_id, request_id):
    store.ingest([Event(
        type=EventType.MODEL_REQUEST_COMPLETED, component=Component.LLM,
        run_id=run_id, summary="completed",
        metadata={"request_id": request_id, "provider": "p", "model": "m",
                  "role": "mutation"})])


def _candidate(store, run_id, cid, score, *, migrant=False, parent_id="p0"):
    store.ingest([Event(
        type=EventType.CANDIDATE_CREATED, component=Component.DATABASE,
        run_id=run_id, candidate_id=cid, summary="added",
        output={"code": f"x = {cid}"},
        metrics={"combined_score": score},
        metadata={"parent_id": parent_id, "migrant": migrant})])


def _arm(store, run_id, requests, scores):
    for i in range(requests):
        _request(store, run_id, f"{run_id}_r{i}")
    for i, s in enumerate(scores):
        _candidate(store, run_id, f"{run_id}_c{i}", s)


# -- the curve --------------------------------------------------------------

def test_best_so_far_never_goes_down(store):
    _arm(store, "run_1", 4, [0.2, 0.9, 0.4, 0.5])
    assert [p["best"] for p in best_so_far(store.reader(), "run_1")] == \
        [0.2, 0.9, 0.9, 0.9]


def test_migrants_do_not_extend_the_curve(store):
    """
    A migrant is a copy of a program already counted. Including them makes a
    run look like it kept improving while it was copying itself between
    islands.
    """
    _arm(store, "run_1", 2, [0.5])
    _candidate(store, "run_1", "m1", 0.5, migrant=True)
    assert len(best_so_far(store.reader(), "run_1")) == 1


def test_area_is_normalised_by_length(store):
    """
    A raw sum rewards the arm that simply ran longer, which is the mistake this
    whole module exists to avoid.
    """
    curve = [{"n": i, "best": 1.0} for i in range(1, 21)]
    assert area_under(curve) == 1.0
    assert area_under(curve[:5]) == 1.0


def test_an_empty_run_has_no_area_rather_than_zero(store):
    """Zero would read as "it found nothing"; there was nothing to find with."""
    assert area_under([]) is None
    assert measure(store.reader(), "run_none")["auc_per_candidate"] is None


def test_reaching_the_best_early_scores_higher_than_reaching_it_late(store):
    """The property that makes area worth measuring at all."""
    _arm(store, "early", 4, [0.9, 0.9, 0.9, 0.9])
    _arm(store, "late", 4, [0.1, 0.1, 0.1, 0.9])

    early = measure(store.reader(), "early")
    late = measure(store.reader(), "late")
    assert early["final_best"] == late["final_best"]
    assert early["auc_per_candidate"] > late["auc_per_candidate"]
    assert early["reached_best_at"] == 1 and late["reached_best_at"] == 4


def test_where_the_run_stopped_improving_is_reported(store):
    """A run that peaked at candidate 3 of 30 spent 90% of its budget confirming."""
    _arm(store, "run_1", 10, [0.1, 0.5, 0.9] + [0.2] * 7)
    assert measure(store.reader(), "run_1")["reached_best_at"] == 3


# -- comparing arms ---------------------------------------------------------

def test_a_thin_comparison_refuses_to_conclude(store):
    _arm(store, "a", 2, [0.5])
    _arm(store, "b", 2, [0.9])
    assert "insufficient evidence" in compare(store.reader(), ["a"], ["b"])["verdict"]


def test_a_real_improvement_is_reported_as_one(store):
    _arm(store, "a", MIN_REQUESTS_PER_ARM, [0.2] * 10)
    _arm(store, "b", MIN_REQUESTS_PER_ARM, [0.9] * 10)

    out = compare(store.reader(), ["a"], ["b"], treatment_name="arm")
    assert "arm is better" in out["verdict"]
    assert "area-under-curve" in out["verdict"]


def test_a_tie_is_called_a_tie(store):
    _arm(store, "a", MIN_REQUESTS_PER_ARM, [0.50] * 10)
    _arm(store, "b", MIN_REQUESTS_PER_ARM, [0.51] * 10)
    assert "no measurable difference" in compare(
        store.reader(), ["a"], ["b"])["verdict"]


def test_climbing_faster_but_ending_lower_is_flagged_as_a_trade_off(store):
    """
    The disagreement worth surfacing: reporting only the winner of one measure
    would hide it entirely.
    """
    _arm(store, "a", MIN_REQUESTS_PER_ARM, [0.9] * 9 + [0.9])       # early, flat
    _arm(store, "b", MIN_REQUESTS_PER_ARM, [0.1] * 9 + [2.0])       # late, higher

    out = compare(store.reader(), ["a"], ["b"])
    assert "disagreement" in out["verdict"]


def test_arms_are_pooled_by_averaging_runs_not_by_gluing_curves(store):
    """
    Two runs are two samples of one configuration. Concatenating their curves
    would invent a single run that never happened.
    """
    _arm(store, "a1", 6, [1.0] * 6)
    _arm(store, "a2", 6, [0.0] * 6)

    pooled = compare(store.reader(), ["a1", "a2"], ["a1"])["baseline"]
    assert pooled["auc_per_candidate"] == pytest.approx(0.5)
    assert pooled["runs_scored"] == 2


def test_an_arm_with_no_scored_candidates_says_so(store):
    _arm(store, "a", MIN_REQUESTS_PER_ARM, [0.5] * 10)
    for i in range(MIN_REQUESTS_PER_ARM):
        _request(store, "b", f"b_r{i}")

    assert "nothing to compare" in compare(store.reader(), ["a"], ["b"])["verdict"]
