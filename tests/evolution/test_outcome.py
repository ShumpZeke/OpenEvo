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
    provider_conditions,
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


# ---------------------------------------------------------------------------
# Provider drift between arms
#
# Ablation arms run one after another, so they do not sample the same provider.
# Within one session Ox Alpha went 40% → 11% success and nemotron went 77% →
# 48% with its p50 latency doubling. An arm that ran through the bad half looks
# worse for reasons that have nothing to do with the feature under test.
# ---------------------------------------------------------------------------

def _arm_with_conditions(store, run_id, ok, failed, scores):
    from control_plane.telemetry.events import Status

    for i in range(ok):
        _request(store, run_id, f"{run_id}_ok{i}")
    for i in range(failed):
        store.ingest([Event(
            type=EventType.MODEL_REQUEST_FAILED, component=Component.LLM,
            run_id=run_id, status=Status.FAILED, summary="failed",
            metadata={"request_id": f"{run_id}_bad{i}", "provider": "p",
                      "model": "m", "role": "mutation"})])
    for i, s in enumerate(scores):
        _candidate(store, run_id, f"{run_id}_c{i}", s)


def test_conditions_are_computed_from_the_run_s_own_requests(store):
    """
    Exact rather than a snapshot of whatever the broker reports now — by the
    time anyone reads the result, "now" is a different provider.
    """
    _arm_with_conditions(store, "a", ok=8, failed=2, scores=[0.5])
    c = provider_conditions(store.reader(), "a")
    assert c["requests"] == 10
    assert c["success_rate"] == 0.8


def test_a_comparison_across_drifted_conditions_is_caveated(store):
    _arm_with_conditions(store, "a", ok=10, failed=1, scores=[0.5] * 10)
    _arm_with_conditions(store, "b", ok=5, failed=6, scores=[0.9] * 10)

    out = compare(store.reader(), ["a"], ["b"], treatment_name="arm")
    assert out["drift"]
    assert "not the same for both arms" in out["verdict"]
    assert "the provider, not the feature" in out["verdict"]


def test_arms_that_faced_the_same_provider_get_no_caveat(store):
    _arm_with_conditions(store, "a", ok=10, failed=1, scores=[0.5] * 10)
    _arm_with_conditions(store, "b", ok=10, failed=1, scores=[0.9] * 10)

    out = compare(store.reader(), ["a"], ["b"])
    assert out["drift"] is None
    assert "CAVEAT" not in out["verdict"]


def test_the_caveat_names_which_arm_had_it_worse(store):
    _arm_with_conditions(store, "good", ok=11, failed=0, scores=[0.5] * 10)
    _arm_with_conditions(store, "bad", ok=3, failed=8, scores=[0.9] * 10)

    out = compare(store.reader(), ["good"], ["bad"],
                  baseline_name="baseline", treatment_name="arm")
    assert "arm ran through worse conditions than baseline" in out["verdict"]


def test_a_run_with_no_requests_has_no_conditions_rather_than_zero(store):
    """Zero success would read as "the provider failed", not "nothing was asked"."""
    c = provider_conditions(store.reader(), "nothing")
    assert c["success_rate"] is None and c["mean_latency_s"] is None


def test_latency_drift_is_caught_when_success_rate_hides_it(store):
    """
    The signal that actually fires. The broker retries, so the engine records a
    *success* for a request the provider failed several times first — run-level
    success rate reads 100% while the broker's own health shows 48%. The cost
    of those retries lands entirely in latency.
    """
    from control_plane.telemetry.events import Component, Event, EventType

    for run_id, latency in (("fast", 118_000.0), ("slow", 583_000.0)):
        for i in range(11):
            store.ingest([Event(
                type=EventType.MODEL_REQUEST_COMPLETED, component=Component.LLM,
                run_id=run_id, duration_ms=latency, summary="completed",
                metadata={"request_id": f"{run_id}_{i}", "provider": "p",
                          "model": "m", "role": "mutation"})])
        for i in range(10):
            _candidate(store, run_id, f"{run_id}_c{i}", 0.5)

    out = compare(store.reader(), ["fast"], ["slow"], treatment_name="arm")
    assert out["drift"]
    assert "did not face the same provider" in out["verdict"]
    assert "cannot tell you which" in out["verdict"]


def test_similar_latencies_raise_no_caveat(store):
    from control_plane.telemetry.events import Component, Event, EventType

    for run_id, latency in (("a", 118_000.0), ("b", 113_700.0)):
        for i in range(11):
            store.ingest([Event(
                type=EventType.MODEL_REQUEST_COMPLETED, component=Component.LLM,
                run_id=run_id, duration_ms=latency, summary="completed",
                metadata={"request_id": f"{run_id}_{i}", "provider": "p",
                          "model": "m", "role": "mutation"})])
        for i in range(10):
            _candidate(store, run_id, f"{run_id}_c{i}", 0.5)

    assert compare(store.reader(), ["a"], ["b"])["drift"] is None
