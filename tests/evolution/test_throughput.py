"""
Candidates per model request.

The number the multi-offspring feature exists to move — and the number it is
easiest to fool yourself with. The first local run at N=3 produced 29
candidates from 12 requests with **zero** novelty-gate rejections, and only 9
distinct code hashes among them. Reported as raw yield that is a 2.4x win;
reported as distinct yield it is 1.29x. These tests pin the honest one.
"""

import pytest

from control_plane.analysis.throughput import MIN_REQUESTS_PER_ARM, compare, measure
from control_plane.storage.store import Store
from control_plane.telemetry.events import Component, Event, EventType, Status


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "cp.db"))
    yield s
    s.close()


def _request(store, run_id, request_id):
    store.ingest([Event(
        type=EventType.MODEL_REQUEST_COMPLETED, component=Component.LLM,
        run_id=run_id, summary="completed",
        metadata={"request_id": request_id, "provider": "opencode_zen",
                  "model": "hy3-free", "role": "mutation"})])


def _candidate(store, run_id, cid, code, *, parent_id="p0", extra=False,
               migrant=False):
    store.ingest([Event(
        type=EventType.CANDIDATE_CREATED, component=Component.DATABASE,
        run_id=run_id, candidate_id=cid, summary="added",
        output={"code": code},
        metadata={"parent_id": parent_id, "multi_offspring": extra,
                  "migrant": migrant})])


def test_the_seed_program_is_not_credited_to_a_request(store):
    """No request generated it, and counting it inflates a short run."""
    _request(store, "run_1", "req_1")
    _candidate(store, "run_1", "seed", "x = 0", parent_id=None)
    _candidate(store, "run_1", "c1", "x = 1")

    m = measure(store.reader(), "run_1")
    assert m["generated_candidates"] == 1
    assert m["candidates_per_request"] == 1.0


def test_migrants_are_not_credited_either(store):
    """A migrant is a copy of something already counted."""
    _request(store, "run_1", "req_1")
    _candidate(store, "run_1", "c1", "x = 1")
    _candidate(store, "run_1", "m1", "x = 1", migrant=True)

    assert measure(store.reader(), "run_1")["generated_candidates"] == 1


def test_duplicate_output_does_not_count_as_useful_yield(store):
    """
    The measurement that forced this definition: three alternatives that are
    the same program are one candidate that cost a request and a half.
    """
    _request(store, "run_1", "req_1")
    for i, code in enumerate(["x = 1", "x = 1", "x = 1"]):
        _candidate(store, "run_1", f"c{i}", code, extra=(i > 0))

    m = measure(store.reader(), "run_1")
    assert m["candidates_per_request"] == 3.0
    assert m["useful_candidates_per_request"] == 1.0
    assert m["duplicate_share"] == pytest.approx(0.667, abs=0.001)


def test_genuinely_different_alternatives_all_count(store):
    _request(store, "run_1", "req_1")
    for i, code in enumerate(["x = 1", "x = 2", "x = 3"]):
        _candidate(store, "run_1", f"c{i}", code, extra=(i > 0))

    m = measure(store.reader(), "run_1")
    assert m["useful_candidates_per_request"] == 3.0
    assert m["duplicate_share"] == 0.0
    assert m["extra_offspring"] == 2


def test_a_run_with_no_requests_reports_absence_not_zero(store):
    """No data is not a yield of zero — that would read as "it produced nothing"."""
    m = measure(store.reader(), "run_empty")
    assert m["mutation_requests"] == 0
    assert m["candidates_per_request"] is None
    assert m["useful_candidates_per_request"] is None
    assert m["duplicate_share"] is None


# -- comparing two arms -----------------------------------------------------

def _arm(store, run_id, requests, codes):
    for i in range(requests):
        _request(store, run_id, f"{run_id}_req_{i}")
    for i, code in enumerate(codes):
        _candidate(store, run_id, f"{run_id}_c{i}", code)


def test_a_thin_comparison_refuses_to_conclude(store):
    _arm(store, "run_a", 2, ["x = 1", "x = 2"])
    _arm(store, "run_b", 2, ["x = 3", "x = 4", "x = 5"])

    out = compare(store.reader(), ["run_a"], ["run_b"])
    assert "insufficient evidence" in out["verdict"]


def test_raw_gain_that_is_all_duplicates_is_called_out(store):
    """
    The exact shape of the local N=3 result: raw yield up, useful yield flat.
    A verdict that reported the raw number would be a lie by omission.
    """
    _arm(store, "run_a", MIN_REQUESTS_PER_ARM, [f"x = {i}" for i in range(10)])
    _arm(store, "run_b", MIN_REQUESTS_PER_ARM,
         [f"y = {i}" for i in range(10)] + ["y = 0"] * 20)

    out = compare(store.reader(), ["run_a"], ["run_b"])
    assert "no useful-yield gain" in out["verdict"]
    assert "duplicates" in out["verdict"]
    assert out["treatment"]["candidates_per_request"] > \
        out["baseline"]["candidates_per_request"]


def test_a_real_gain_is_reported_with_both_numbers(store):
    _arm(store, "run_a", MIN_REQUESTS_PER_ARM, [f"x = {i}" for i in range(10)])
    _arm(store, "run_b", MIN_REQUESTS_PER_ARM, [f"y = {i}" for i in range(25)])

    out = compare(store.reader(), ["run_a"], ["run_b"])
    assert "useful yield" in out["verdict"]
    assert "raw yield" in out["verdict"]


def test_pooling_sums_the_arms(store):
    _arm(store, "run_a1", 6, [f"x = {i}" for i in range(6)])
    _arm(store, "run_a2", 6, [f"z = {i}" for i in range(6)])

    out = compare(store.reader(), ["run_a1", "run_a2"], ["run_a1"])
    assert out["baseline"]["mutation_requests"] == 12
    assert out["baseline"]["generated_candidates"] == 12


def test_forged_variants_do_not_count_towards_yield_per_request(store):
    """
    They have a parent, so they look like offspring — and they cost no model
    request at all. Counting them inflates candidates-per-request for exactly
    the arm that adds them, so the seed-forge ablation would have reported a
    gain it did not earn. Caught by running that arm and reading the number.
    """
    _request(store, "run_1", "req_1")
    _candidate(store, "run_1", "c1", "x = 1")
    for i in range(3):
        store.ingest([Event(
            type=EventType.CANDIDATE_CREATED, component=Component.DATABASE,
            run_id="run_1", candidate_id=f"f{i}", summary="forged",
            output={"code": f"forged = {i}"},
            metadata={"parent_id": "seed", "seed_forge": True,
                      "forge_origin": "scale_effort"})])

    m = measure(store.reader(), "run_1")
    assert m["candidates_per_request"] == 1.0
    assert m["forged"] == 3
    assert m["candidates"] == 4, "they are still in the population"


def test_forged_variants_still_count_towards_the_outcome(store):
    """
    They are real population members and can win. Excluding them from the
    best-so-far curve would hide the effect the feature is claimed to have.
    """
    from control_plane.analysis.outcome import measure as outcome

    _request(store, "run_1", "req_1")
    _candidate(store, "run_1", "c1", "x = 1")
    store.ingest([Event(
        type=EventType.CANDIDATE_CREATED, component=Component.DATABASE,
        run_id="run_1", candidate_id="f0", summary="forged",
        output={"code": "forged"}, metrics={"combined_score": 0.99},
        metadata={"parent_id": "seed", "seed_forge": True})])

    assert outcome(store.reader(), "run_1")["final_best"] == 0.99
