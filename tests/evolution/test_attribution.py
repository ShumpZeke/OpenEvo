"""
Candidate-to-model-request attribution.

Upstream never attaches a candidate id to the generating call — measured: 0 of
22 stored model requests carried one — so no post-hoc join can recover which
route produced which candidate. It is captured at generation time instead.

The test that matters is concurrency: OpenEvolve runs iterations as concurrent
asyncio tasks, and a module-level global would silently mis-attribute under
exactly that load.
"""
import asyncio
import contextvars
import time

import pytest

from control_plane.telemetry import instrument as inst


def _set(request_id, provider="opencode_zen", model="x-preview-f-free", at=None):
    inst._generating_request.set({
        "request_id": request_id, "provider": provider, "model": model,
        "latency_ms": 100.0, "tokens": 500, "reasoning_tokens": 400,
        "at": at if at is not None else time.time(),
    })


def test_context_is_empty_by_default():
    assert inst._generating_request.get() is None


@pytest.mark.asyncio
async def test_concurrent_tasks_do_not_see_each_others_request():
    """
    The failure a global would cause. Two iterations in flight, each generating
    from a different route: each must read back its own request, not the other's
    and not whichever finished last.
    """
    seen = {}

    async def iteration(name, request_id, model, delay):
        _set(request_id, model=model)
        await asyncio.sleep(delay)          # evaluation happens here
        got = inst._generating_request.get()
        seen[name] = (got["request_id"], got["model"])

    # Deliberately interleaved: B sets its request while A is suspended.
    await asyncio.gather(
        iteration("a", "req_a", "x-preview-f-free", 0.03),
        iteration("b", "req_b", "nemotron-3-ultra-free", 0.01),
    )

    assert seen["a"] == ("req_a", "x-preview-f-free")
    assert seen["b"] == ("req_b", "nemotron-3-ultra-free")


@pytest.mark.asyncio
async def test_child_task_inherits_the_parent_context():
    """Evaluation may be awaited in a child task; attribution must survive it."""
    _set("req_parent")

    async def child():
        return inst._generating_request.get()["request_id"]

    assert await asyncio.create_task(child()) == "req_parent"


@pytest.mark.asyncio
async def test_a_task_cannot_leak_its_request_back_to_the_caller():
    async def inner():
        _set("req_inner")

    await asyncio.create_task(inner())
    # The child set it in its own copied context; the parent must be unaffected.
    assert inst._generating_request.get() is None


def test_stale_attribution_is_rejected():
    """
    A candidate added long after the last request in this context — a migrant
    copy, a checkpoint reload — must not be credited to an unrelated request.
    """
    old = time.time() - (inst._ATTRIBUTION_MAX_AGE_S + 60)
    _set("req_ancient", at=old)
    req = inst._generating_request.get()
    assert (time.time() - req["at"]) > inst._ATTRIBUTION_MAX_AGE_S


def test_fresh_attribution_is_accepted():
    _set("req_now")
    req = inst._generating_request.get()
    assert (time.time() - req["at"]) < inst._ATTRIBUTION_MAX_AGE_S


def test_attribution_window_is_generous_enough_for_a_slow_evaluator():
    """
    Ox Alpha averaged 130-229s per request and evaluation follows it. A tight
    window would drop legitimate attributions on exactly the primary route.
    """
    assert inst._ATTRIBUTION_MAX_AGE_S >= 600


def test_store_persists_generation_provenance(tmp_path):
    from control_plane.storage.store import Store
    from control_plane.telemetry.events import Component, Event, EventType

    s = Store(str(tmp_path / "t.db"))
    s.ingest([Event(
        type=EventType.CANDIDATE_CREATED, component=Component.DATABASE,
        run_id="r1", candidate_id="c1", metrics={"combined_score": 0.5},
        metadata={
            "generating_request_id": "mreq_1",
            "generating_provider": "opencode_zen",
            "generating_model": "x-preview-f-free",
            "generating_latency_ms": 1234.0,
            "generating_tokens": 8000,
        },
    )])
    row = s.query_one("SELECT * FROM candidates WHERE candidate_id='c1'")
    assert row["gen_request_id"] == "mreq_1"
    assert row["gen_provider"] == "opencode_zen"
    assert row["gen_model"] == "x-preview-f-free"
    assert row["gen_tokens"] == 8000


def test_candidates_without_attribution_are_null_not_guessed(tmp_path):
    """An unattributed candidate reads as unknown, never as a default route."""
    from control_plane.storage.store import Store
    from control_plane.telemetry.events import Component, Event, EventType

    s = Store(str(tmp_path / "t.db"))
    s.ingest([Event(type=EventType.CANDIDATE_CREATED, component=Component.DATABASE,
                    run_id="r1", candidate_id="c2")])
    row = s.query_one("SELECT * FROM candidates WHERE candidate_id='c2'")
    assert row["gen_provider"] is None and row["gen_model"] is None


# ---------------------------------------------------------------------------
# The process boundary
#
# The ContextVar above is correct and was still not enough. In the default
# `process_parallel` path the model request is made in a *worker process* and
# `database.add` runs in the *main* process, which receives only a pickled
# `SerializableResult`. A live 4-iteration run through the broker produced
# 3 candidates and 0 attributed ones: nothing in memory crosses fork.
#
# `Program.metadata` does cross, because it is a dataclass field that survives
# `to_dict()` → pickle → `Program(**dict)`. These tests pin that path.
# ---------------------------------------------------------------------------

import pickle
import types


class _FakeResult:
    """Stands in for SerializableResult, which is what the worker returns."""

    def __init__(self, child_program_dict=None, error=None):
        self.child_program_dict = child_program_dict
        self.error = error


def _record(request_id="req_worker", provider="opencode_zen",
            model="x-preview-f-free", at=None):
    return {
        "request_id": request_id, "provider": provider, "model": model,
        "latency_ms": 229_000.0, "tokens": 8000, "reasoning_tokens": 7990,
        "at": at if at is not None else time.time(),
    }


@pytest.fixture
def clean_worker_slot():
    """
    Reset both channels. The ContextVar is process-wide for synchronous tests,
    so a value set by an earlier test would silently satisfy the fallback path
    and hide a metadata bug.
    """
    inst._worker_generation = None
    inst._worker_generation_pid = None
    token = inst._generating_request.set(None)
    yield
    inst._generating_request.reset(token)
    inst._worker_generation = None
    inst._worker_generation_pid = None


def test_metadata_beats_the_contextvar(clean_worker_slot):
    """
    The metadata stamp is exact — the worker that made the call also built the
    program — so it must win over a ContextVar that may belong to another task.
    """
    _set("req_from_context")
    program = types.SimpleNamespace(
        metadata={inst.ATTRIBUTION_KEY: _record("req_from_worker")})

    assert inst._attribution_of(program)["request_id"] == "req_from_worker"


def test_migrants_are_not_attributed_twice(clean_worker_slot):
    """
    `_migrate_programs` copies metadata wholesale into the migrant. Crediting
    the route a second time would inflate exactly the attempt count and yield
    that route_quality compares routes on.
    """
    program = types.SimpleNamespace(metadata={
        inst.ATTRIBUTION_KEY: _record("req_original"),
        "migrant": True,
    })
    assert inst._attribution_of(program) is None


def test_a_candidate_with_no_provenance_is_null_not_guessed(clean_worker_slot):
    assert inst._attribution_of(types.SimpleNamespace(metadata={})) is None


def test_stale_context_attribution_is_refused(clean_worker_slot):
    _set("req_ancient", at=time.time() - (inst._ATTRIBUTION_MAX_AGE_S + 60))
    assert inst._attribution_of(types.SimpleNamespace(metadata={})) is None


def test_first_request_of_an_iteration_wins(clean_worker_slot):
    """
    The generating call is the first LLM call of a worker iteration; a later
    one is the evaluator's LLM feedback. Crediting a mutation to the request
    that *judged* it would be worse than leaving it unattributed.
    """
    inst._begin_worker_attribution()
    inst._publish_generation(_record("req_generation"))
    inst._publish_generation(_record("req_evaluator_feedback"))

    assert inst._take_worker_attribution()["request_id"] == "req_generation"


def test_the_slot_is_consumed_so_the_next_iteration_starts_clean(clean_worker_slot):
    inst._begin_worker_attribution()
    inst._publish_generation(_record("req_one"))
    assert inst._take_worker_attribution()["request_id"] == "req_one"
    assert inst._take_worker_attribution() is None


def test_a_failed_generation_leaves_nothing_to_misattribute(clean_worker_slot):
    """A worker whose LLM call raised must not inherit the last one's record."""
    inst._begin_worker_attribution()
    inst._publish_generation(_record("req_previous_iteration"))
    inst._take_worker_attribution()

    inst._begin_worker_attribution()          # next iteration; LLM call fails
    assert inst._take_worker_attribution() is None


def test_worker_wrapper_stamps_the_returned_program(monkeypatch, clean_worker_slot):
    from openevolve import process_parallel

    def fake_worker(iteration, db_snapshot, parent_id, inspiration_ids):
        # What the real worker does between these two points: call the model.
        inst._publish_generation(_record("req_in_worker"))
        return _FakeResult(child_program_dict={"id": "child-1", "code": "x = 1",
                                               "metadata": {"island": 0}})

    monkeypatch.setattr(process_parallel, "_run_iteration_worker", fake_worker)
    inst.install_worker_attribution_hook()

    result = process_parallel._run_iteration_worker(1, {}, "parent-1", [])
    stamped = result.child_program_dict["metadata"][inst.ATTRIBUTION_KEY]

    assert stamped["request_id"] == "req_in_worker"
    assert stamped["model"] == "x-preview-f-free"
    # Upstream's own metadata must be preserved, not replaced.
    assert result.child_program_dict["metadata"]["island"] == 0


def test_worker_wrapper_survives_an_errored_iteration(monkeypatch, clean_worker_slot):
    from openevolve import process_parallel

    def fake_worker(*a, **kw):
        return _FakeResult(error="No valid diffs found in response")

    monkeypatch.setattr(process_parallel, "_run_iteration_worker", fake_worker)
    inst.install_worker_attribution_hook()

    result = process_parallel._run_iteration_worker(1, {}, "parent-1", [])
    assert result.error and result.child_program_dict is None


def test_the_wrapper_is_still_picklable_by_reference(monkeypatch, clean_worker_slot):
    """
    ProcessPoolExecutor pickles the callable by qualified name and resolves it
    in the child. If the wrapper did not rebind the module attribute to itself,
    pickling would raise and every parallel run would die at submit().
    """
    from openevolve import process_parallel

    # Wrap the *real* function: the property under test is about its module
    # and qualname, which a locally-defined stand-in would not have. Setting
    # the attribute to its own current value registers it for restoration.
    monkeypatch.setattr(process_parallel, "_run_iteration_worker",
                        process_parallel._run_iteration_worker)
    inst.install_worker_attribution_hook()

    wrapper = process_parallel._run_iteration_worker
    assert getattr(wrapper, "__evolution_instrumented__", False)
    assert pickle.loads(pickle.dumps(wrapper)) is wrapper


def test_installing_twice_does_not_double_wrap(monkeypatch, clean_worker_slot):
    from openevolve import process_parallel

    calls = []

    def fake_worker(*a, **kw):
        calls.append(1)
        return _FakeResult()

    monkeypatch.setattr(process_parallel, "_run_iteration_worker", fake_worker)
    inst.install_worker_attribution_hook()
    first = process_parallel._run_iteration_worker
    inst.install_worker_attribution_hook()

    assert process_parallel._run_iteration_worker is first
    process_parallel._run_iteration_worker(1, {}, "p", [])
    assert len(calls) == 1


def test_attribution_survives_the_real_serialization_path(monkeypatch, clean_worker_slot):
    """
    End-to-end over the boundary, without spawning a process: worker stamps →
    pickle (what the executor does) → Program(**dict) (what the main process
    does at process_parallel.py:616) → _attribution_of (what _after_add reads).
    """
    from openevolve.database import Program
    from openevolve import process_parallel

    def fake_worker(*a, **kw):
        inst._publish_generation(_record("req_round_trip"))
        child = Program(id="child-2", code="x = 2", parent_id="parent-1",
                        metadata={"changes": "tweak", "island": 1})
        return _FakeResult(child_program_dict=child.to_dict())

    monkeypatch.setattr(process_parallel, "_run_iteration_worker", fake_worker)
    inst.install_worker_attribution_hook()

    result = process_parallel._run_iteration_worker(1, {}, "parent-1", [])
    # The executor pickles the result back to the main process.
    revived = pickle.loads(pickle.dumps(result))
    program = Program(**revived.child_program_dict)

    rec = inst._attribution_of(program)
    assert rec["request_id"] == "req_round_trip"
    assert rec["provider"] == "opencode_zen"
    assert program.metadata["island"] == 1        # upstream's fields intact


# ---------------------------------------------------------------------------
# Reading the broker's provenance stamp off a response
# ---------------------------------------------------------------------------

def test_broker_route_is_read_from_a_pydantic_extra_field():
    """
    How it actually arrives: the OpenAI client parses the response into a model
    and keeps unrecognised fields in `model_extra`.
    """
    resp = types.SimpleNamespace(
        model="x-preview-f-free",
        model_extra={"oe_max": {"provider": "opencode_zen", "model": "x-preview-f-free",
                                "attempt": 1, "reasoning_tokens": 7990}})
    route = inst._broker_route(resp)
    assert route["provider"] == "opencode_zen"
    assert route["reasoning_tokens"] == 7990


def test_broker_route_is_read_from_a_plain_dict():
    route = inst._broker_route({"oe_max": {"provider": "opencode_zen", "model": "hy3-free"}})
    assert route["model"] == "hy3-free"


def test_a_direct_provider_response_has_no_route_stamp():
    """Calling a provider directly must not be given a route it never had."""
    assert inst._broker_route(types.SimpleNamespace(model="nemotron-super-49b")) is None
    assert inst._broker_route({"id": "chatcmpl-1", "model": "gpt-4"}) is None
    assert inst._broker_route(None) is None


def test_a_malformed_stamp_is_ignored_rather_than_half_used():
    """A stamp with no model would produce a route key like 'zen/None'."""
    assert inst._broker_route({"oe_max": {"provider": "opencode_zen"}}) is None
    assert inst._broker_route({"oe_max": "not-a-dict"}) is None
