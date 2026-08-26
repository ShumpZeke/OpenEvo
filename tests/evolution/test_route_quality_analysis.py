"""
Turning stored telemetry into a route comparison.

`oe_max.route_quality` defines what the measures mean; this is the part that
decides what counts as an attempt, and getting that wrong is how a route ends
up looking better than it is.

The rule under test: the attempt set is the set of mutation-role *model
requests*, not the set of candidates. A route that burns 292 seconds and
returns an unusable diff produced no candidate at all — counting candidates
would make its worst outcome invisible.
"""
import json
import time

import pytest

from control_plane.analysis.route_quality import (
    analyse, analyse_runs, attribution_coverage, build_tracker,
)
from control_plane.storage.store import Store
from control_plane.telemetry.events import Component, Event, EventType, Status


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "cp.db"))
    yield s
    s.close()


def _request(store, run_id, request_id, provider, model, *, status=Status.OK,
             latency_ms=1000.0, tokens=500, role="mutation", reasoning=0):
    store.ingest([Event(
        type=EventType.MODEL_REQUEST_COMPLETED if status is Status.OK
        else EventType.MODEL_REQUEST_FAILED,
        component=Component.LLM, run_id=run_id, status=status,
        summary=f"model request {request_id}",
        metrics={"total_tokens": tokens},
        metadata={"request_id": request_id, "provider": provider, "model": model,
                  "role": role, "reasoning_tokens": reasoning},
    )])


def _candidate(store, run_id, candidate_id, request_id, provider, model,
               *, score=0.5, parent_id=None):
    store.ingest([Event(
        type=EventType.CANDIDATE_CREATED, component=Component.DATABASE,
        run_id=run_id, candidate_id=candidate_id,
        summary=f"candidate {candidate_id} added",
        metrics={"combined_score": score},
        metadata={"parent_id": parent_id,
                  "generating_request_id": request_id,
                  "generating_provider": provider,
                  "generating_model": model,
                  "generating_latency_ms": 1000.0,
                  "generating_tokens": 500},
    )])


def _rejection(store, run_id, candidate_id, request_id, provider, model):
    store.ingest([Event(
        type=EventType.CANDIDATE_REJECTED, component=Component.DATABASE,
        run_id=run_id, candidate_id=candidate_id, status=Status.REJECTED,
        summary=f"candidate {candidate_id} rejected",
        metadata={"reason": "novelty_or_duplicate",
                  "generating_request_id": request_id,
                  "generating_provider": provider,
                  "generating_model": model},
    )])


def test_a_request_that_produced_nothing_is_still_an_attempt(store):
    """
    The measurement that motivated all of this: Ox Alpha at a 292s p50 and a
    29% success rate. If only candidates counted, those requests would vanish
    from the numbers and the route would look far better than it is.
    """
    _request(store, "run_1", "req_1", "opencode_zen", "x-preview-f-free")

    stats = build_tracker(store.reader(), "run_1").routes["opencode_zen/x-preview-f-free"]
    assert stats.attempts == 1
    assert stats.unparseable == 1
    assert stats.accepted == 0
    assert stats.validity_rate == 0.0


def test_a_failed_request_is_charged_but_not_called_unparseable(store):
    """
    A timeout and an inapplicable diff are both worth an attempt, and they are
    different problems: one is the route, the other is the model.
    """
    _request(store, "run_1", "req_1", "opencode_zen", "x-preview-f-free",
             status=Status.FAILED, latency_ms=600_000.0)

    stats = build_tracker(store.reader(), "run_1").routes["opencode_zen/x-preview-f-free"]
    assert (stats.attempts, stats.failures, stats.unparseable) == (1, 1, 0)
    assert stats.failure_rate == 1.0


def test_a_rejected_candidate_counts_as_a_duplicate_not_a_silence(store):
    """
    Rejected candidates never reach the `candidates` projection, so the only
    trace is the event. Without reading it, a duplicate-heavy route would be
    indistinguishable from one that returned nothing.
    """
    _request(store, "run_1", "req_1", "opencode_zen", "hy3-free")
    _rejection(store, "run_1", "cand_1", "req_1", "opencode_zen", "hy3-free")

    stats = build_tracker(store.reader(), "run_1").routes["opencode_zen/hy3-free"]
    assert (stats.duplicates, stats.unparseable, stats.accepted) == (1, 0, 0)
    assert stats.duplicate_rate == 1.0


def test_fitness_delta_is_measured_against_the_real_parent(store):
    _request(store, "run_1", "req_p", "opencode_zen", "hy3-free")
    _candidate(store, "run_1", "parent", "req_p", "opencode_zen", "hy3-free", score=0.40)
    _request(store, "run_1", "req_c", "opencode_zen", "hy3-free")
    _candidate(store, "run_1", "child", "req_c", "opencode_zen", "hy3-free",
               score=0.55, parent_id="parent")

    stats = build_tracker(store.reader(), "run_1").routes["opencode_zen/hy3-free"]
    assert stats.improvements == 1
    assert stats.best_delta == pytest.approx(0.15)


def test_a_candidate_with_no_parent_score_is_not_scored_as_an_improvement(store):
    """The seed program has no parent; crediting it would invent a delta."""
    _request(store, "run_1", "req_1", "opencode_zen", "hy3-free")
    _candidate(store, "run_1", "cand_1", "req_1", "opencode_zen", "hy3-free", score=0.9)

    stats = build_tracker(store.reader(), "run_1").routes["opencode_zen/hy3-free"]
    assert stats.accepted == 1
    assert stats.improvements == 0
    assert stats.total_positive_delta == 0.0


def test_evaluator_feedback_requests_are_not_charged_as_mutations(store):
    """
    `use_llm_feedback` sends a second kind of request through the same client.
    Counting it would charge a route for grading someone else's candidate.
    """
    _request(store, "run_1", "req_gen", "opencode_zen", "hy3-free", role="mutation")
    _candidate(store, "run_1", "cand_1", "req_gen", "opencode_zen", "hy3-free")
    _request(store, "run_1", "req_eval", "opencode_zen", "hy3-free", role="evaluation")

    stats = build_tracker(store.reader(), "run_1").routes["opencode_zen/hy3-free"]
    assert stats.attempts == 1


def test_two_routes_are_kept_apart(store):
    _request(store, "run_1", "req_a", "opencode_zen", "x-preview-f-free",
             latency_ms=292_000.0, tokens=8000)
    _request(store, "run_1", "req_b", "opencode_zen", "nemotron-3-ultra-free",
             latency_ms=112_000.0, tokens=2000)
    _candidate(store, "run_1", "cand_b", "req_b", "opencode_zen", "nemotron-3-ultra-free")

    routes = build_tracker(store.reader(), "run_1").routes
    assert set(routes) == {"opencode_zen/x-preview-f-free",
                           "opencode_zen/nemotron-3-ultra-free"}
    assert routes["opencode_zen/x-preview-f-free"].accepted == 0
    assert routes["opencode_zen/nemotron-3-ultra-free"].accepted == 1


def test_coverage_says_when_a_run_predates_attribution(store):
    """
    An older run has no provenance on any candidate. It must say so rather than
    return an empty comparison that reads like "no route was any good".
    """
    store.ingest([Event(
        type=EventType.CANDIDATE_CREATED, component=Component.DATABASE,
        run_id="run_old", candidate_id="cand_1", summary="candidate added",
        metadata={"parent_id": None},
    )])

    cov = attribution_coverage(store.reader(), "run_old")
    assert cov["candidates"] == 1 and cov["attributed"] == 0
    assert "predates attribution" in cov["note"]


def test_coverage_explains_the_expected_shortfall(store):
    _request(store, "run_1", "req_1", "opencode_zen", "hy3-free")
    _candidate(store, "run_1", "cand_1", "req_1", "opencode_zen", "hy3-free")
    store.ingest([Event(   # the seed program: no generating request, by nature
        type=EventType.CANDIDATE_CREATED, component=Component.DATABASE,
        run_id="run_1", candidate_id="seed", summary="candidate added",
        metadata={"parent_id": None},
    )])

    cov = attribution_coverage(store.reader(), "run_1")
    assert (cov["candidates"], cov["attributed"]) == (2, 1)
    assert "unattributable by design" in cov["note"]


def test_a_thin_run_refuses_to_recommend(store):
    """The failure mode this whole module guards against: a confident answer."""
    _request(store, "run_1", "req_a", "opencode_zen", "x-preview-f-free")
    _candidate(store, "run_1", "cand_a", "req_a", "opencode_zen", "x-preview-f-free")

    out = analyse(store.reader(), "run_1")
    assert "insufficient evidence" in out["comparison"]["verdict"]
    assert out["comparison"]["excluded_insufficient_data"]


def test_pooling_runs_sums_the_attempts_and_names_every_run(store):
    for run in ("run_1", "run_2"):
        for i in range(3):
            _request(store, run, f"{run}_req_{i}", "opencode_zen", "hy3-free")

    out = analyse_runs(store.reader(), ["run_1", "run_2"], min_attempts=5)
    assert out["routes"]["opencode_zen/hy3-free"]["attempts"] == 6
    assert [c["run_id"] for c in out["coverage"]] == ["run_1", "run_2"]
    # Pooled, the route now clears the bar a single run could not.
    assert not out["comparison"]["excluded_insufficient_data"]


def test_pooling_keeps_the_best_delta_rather_than_adding_them(store):
    for run, score in (("run_1", 0.30), ("run_2", 0.70)):
        _request(store, run, f"{run}_p", "opencode_zen", "hy3-free")
        _candidate(store, run, f"{run}_parent", f"{run}_p", "opencode_zen", "hy3-free",
                   score=0.10)
        _request(store, run, f"{run}_c", "opencode_zen", "hy3-free")
        _candidate(store, run, f"{run}_child", f"{run}_c", "opencode_zen", "hy3-free",
                   score=score, parent_id=f"{run}_parent")

    out = analyse_runs(store.reader(), ["run_1", "run_2"])
    assert out["routes"]["opencode_zen/hy3-free"]["best_delta"] == pytest.approx(0.60)


def test_an_unknown_run_is_empty_not_an_error(store):
    out = analyse(store.reader(), "run_does_not_exist")
    assert out["routes"] == {}
    assert out["coverage"]["candidates"] == 0


# ---------------------------------------------------------------------------
# The broker alias
#
# OpenEvolve is pointed at the OE-MAX broker and only ever names the alias
# `oe-max-primary`. Recorded naively, every route through the broker collapses
# into one row called "local/oe-max-primary" — in exactly the configuration the
# project ships — and no route comparison is possible at all.
# ---------------------------------------------------------------------------

def _request_pair(store, run_id, request_id, *, requested=("local", "oe-max-primary"),
                  served=("opencode_zen", "x-preview-f-free"), latency_ms=294_846.0,
                  tokens=15_821, reasoning=0):
    """A request as it is really recorded: asked for by alias, served by a route."""
    store.ingest([Event(
        type=EventType.MODEL_REQUEST_STARTED, component=Component.LLM,
        run_id=run_id, status=Status.RUNNING, summary="model request started",
        metadata={"request_id": request_id, "provider": requested[0],
                  "model": requested[1], "role": "mutation"},
    )])
    store.ingest([Event(
        type=EventType.MODEL_REQUEST_COMPLETED, component=Component.LLM,
        run_id=run_id, duration_ms=latency_ms, summary="model request completed",
        metrics={"total_tokens": tokens},
        metadata={"request_id": request_id, "provider": served[0], "model": served[1],
                  "requested_provider": requested[0], "requested_model": requested[1],
                  "role": "mutation", "reasoning_tokens": reasoning},
    )])


def test_the_serving_route_wins_over_the_alias(store):
    _request_pair(store, "run_1", "req_1")

    routes = build_tracker(store.reader(), "run_1").routes
    assert list(routes) == ["opencode_zen/x-preview-f-free"]
    assert "local/oe-max-primary" not in routes


def test_the_alias_is_kept_rather_than_overwritten(store):
    """
    Replacing the requested model would lose what the engine actually asked
    for, which is the thing to check when a route substitution looks wrong.
    """
    _request_pair(store, "run_1", "req_1")

    md = json.loads(store.reader().execute(
        "SELECT metadata FROM model_requests WHERE request_id = 'req_1'").fetchone()[0])
    assert md["requested_model"] == "oe-max-primary"
    assert md["model"] == "x-preview-f-free"


def test_two_broker_routes_are_told_apart_despite_one_alias(store):
    """The comparison T1 needs: same alias, different work, different rows."""
    _request_pair(store, "run_1", "req_slow", latency_ms=294_846.0, tokens=15_821,
                  served=("opencode_zen", "x-preview-f-free"))
    _request_pair(store, "run_1", "req_fast", latency_ms=112_000.0, tokens=2_000,
                  served=("opencode_zen", "nemotron-3-ultra-free"))
    _candidate(store, "run_1", "cand_fast", "req_fast",
               "opencode_zen", "nemotron-3-ultra-free")

    routes = build_tracker(store.reader(), "run_1").routes
    assert routes["opencode_zen/x-preview-f-free"].accepted == 0
    assert routes["opencode_zen/nemotron-3-ultra-free"].accepted == 1
    assert routes["opencode_zen/x-preview-f-free"].mean_latency_s == pytest.approx(294.8, abs=0.1)


def test_hidden_reasoning_tokens_are_recorded(store):
    """
    Ox Alpha was measured spending 7,986-7,997 of an 8,000-token budget on
    hidden reasoning. A route's cost is invisible without it.
    """
    _request_pair(store, "run_1", "req_1", tokens=8_000, reasoning=7_990)

    stats = build_tracker(store.reader(), "run_1").routes["opencode_zen/x-preview-f-free"]
    assert stats.total_reasoning_tokens == 7_990
    assert stats.reasoning_share == pytest.approx(0.99875)


def test_a_direct_provider_call_is_unaffected(store):
    """The override is additive: no broker, no substitution."""
    _request(store, "run_1", "req_1", "nvidia_nim", "nemotron-super-49b")

    assert list(build_tracker(store.reader(), "run_1").routes) == \
        ["nvidia_nim/nemotron-super-49b"]


# ---------------------------------------------------------------------------
# Run progress
#
# Upstream emits no generation boundary, so `runs.iterations_done` sat at 0 for
# the whole of every run — a progress bar reading "0 / 12" while the run is
# plainly working. A wrong number is worse than a missing one.
# ---------------------------------------------------------------------------

def _start_run(store, run_id):
    store.ingest([Event(
        type=EventType.EXPERIMENT_CREATED, component=Component.CONTROL_PLANE,
        run_id=run_id, experiment_id=f"exp_{run_id}", summary="run created",
        metadata={"name": run_id})])


def _iteration_done(store, run_id, iteration, *, error=None):
    store.ingest([Event(
        type=EventType.GENERATION_COMPLETED, component=Component.CONTROLLER,
        run_id=run_id, iteration=iteration,
        status=Status.FAILED if error else Status.OK,
        summary=f"iteration {iteration} completed",
        metadata={"produced_candidate": error is None, "error": error},
    )])


def _run_row(store, run_id):
    return store.reader().execute(
        "SELECT iterations_done FROM runs WHERE run_id = ?", (run_id,)).fetchone()


def test_progress_counts_iterations_not_indices(store):
    """
    Upstream numbers iterations from 0, so finishing index 11 of a 12-iteration
    run means 12 are done — not 11.
    """
    _start_run(store, "run_1")
    for i in range(12):
        _iteration_done(store, "run_1", i)

    assert _run_row(store, "run_1")["iterations_done"] == 12


def test_the_very_first_iteration_registers(store):
    """Iteration 0 is falsy; a truthiness test would drop it silently."""
    _start_run(store, "run_1")
    _iteration_done(store, "run_1", 0)

    assert _run_row(store, "run_1")["iterations_done"] == 1


def test_an_iteration_that_produced_nothing_still_counts_as_progress(store):
    """
    "No valid diffs found" is a completed iteration: it consumed a request and
    real time. Counting only successful ones makes a degraded route look like a
    stalled run.
    """
    _start_run(store, "run_1")
    _iteration_done(store, "run_1", 0, error="No valid diffs found in response")
    _iteration_done(store, "run_1", 1)

    assert _run_row(store, "run_1")["iterations_done"] == 2


def test_out_of_order_completions_do_not_move_progress_backwards(store):
    """Iterations run in parallel and finish out of order."""
    _start_run(store, "run_1")
    for i in (5, 1, 3, 0):
        _iteration_done(store, "run_1", i)

    assert _run_row(store, "run_1")["iterations_done"] == 6
