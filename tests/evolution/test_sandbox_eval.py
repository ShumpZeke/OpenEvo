"""
Evaluating candidates in a sandbox instead of in the evaluator's own process.

Upstream calls the task's evaluation function on a thread, so an evolved
program — code nobody wrote and nobody reviewed — runs inside the run with no
ceilings. These tests use the shipped example task and real candidate files,
because the thing being checked is whether a hostile program is actually
stopped, and a stubbed evaluator cannot answer that.
"""

import os

import pytest

from control_plane.telemetry import sandbox_eval
from oe_max.execution import available_backends

EVALUATOR = "examples/function_minimization/evaluator.py"
SEED = "examples/function_minimization/initial_program.py"

pytestmark = pytest.mark.skipif(
    "subprocess" not in available_backends(),
    reason="POSIX resource limits unavailable on this platform")


@pytest.fixture(autouse=True)
def _pin_the_subprocess_backend(monkeypatch):
    """
    These tests are about evaluation *semantics*, not about containerisation.

    Left on "auto" they run against whichever backend is available, which on a
    machine with Docker means a `python:3.11-slim` container — an image with no
    numpy, so the shipped example's evaluator cannot even be imported and every
    honest candidate "crashes". That is a true statement about that image and a
    useless one about the evaluation path, and it turned CI red for a reason
    unrelated to what these tests check.

    The container backend has its own coverage: `tests/oe_max/test_sandbox_mounts.py`
    for what it exposes, and `OE_MAX_SANDBOX_IMAGE` for pointing it at an image
    that carries a task's dependencies (SANDBOX.md).
    """
    monkeypatch.setenv(sandbox_eval.ENV_BACKEND, "subprocess")


OBJECTIVE = '''
import numpy as np


def evaluate_function(x, y):
    return np.sin(x) * np.cos(y) + np.sin(x * y) + (x**2 + y**2) / 20
'''


def _candidate(tmp_path, body, name="candidate.py"):
    path = tmp_path / name
    path.write_text(OBJECTIVE + body)
    return str(path)


# -- configuration ----------------------------------------------------------

def test_it_is_off_unless_asked_for(monkeypatch):
    monkeypatch.delenv(sandbox_eval.ENV_SANDBOX_EVAL, raising=False)
    assert sandbox_eval.enabled() is False
    assert sandbox_eval.install(None) is False


def test_install_reports_whether_it_actually_installed(monkeypatch):
    """
    A caller that asked for a sandbox must be able to tell it did not get one,
    rather than assume.
    """
    monkeypatch.setenv(sandbox_eval.ENV_SANDBOX_EVAL, "1")
    from openevolve.evaluator import Evaluator

    original = Evaluator._direct_evaluate
    try:
        assert sandbox_eval.install(None) is True
        # Already wrapped: a second install must not double-wrap.
        assert sandbox_eval.install(None) is False
    finally:
        Evaluator._direct_evaluate = original


# -- an honest candidate still scores the same -----------------------------

def test_the_shipped_seed_program_evaluates_normally():
    payload, result = sandbox_eval.evaluate_in_sandbox(
        EVALUATOR, SEED, "evaluate", 90.0)
    assert result.ok, result.to_dict()
    metrics = payload["metrics"]
    assert metrics["combined_score"] > 0
    assert set(metrics) >= {"value_score", "distance_score", "combined_score"}


def test_the_evaluators_own_imports_still_work():
    """
    Upstream adds the evaluator's directory to sys.path before loading it; the
    sandbox script has to do the same or every task with a local helper breaks.
    """
    _, result = sandbox_eval.evaluate_in_sandbox(EVALUATOR, SEED, "evaluate", 90.0)
    assert result.ok
    assert "ModuleNotFoundError" not in result.stderr


# -- a hostile candidate is stopped ----------------------------------------

def test_an_unbounded_allocation_does_not_take_the_evaluator_with_it(tmp_path, monkeypatch):
    monkeypatch.setenv(sandbox_eval.ENV_MEMORY_MB, "256")
    path = _candidate(tmp_path, '''

def search_algorithm(iterations=1000, bounds=(-5, 5)):
    chunks = []
    while True:
        chunks.append(bytearray(32 * 1024 * 1024))


def run_search():
    return search_algorithm()
''')
    _, result = sandbox_eval.evaluate_in_sandbox(EVALUATOR, path, "evaluate", 20.0)
    assert not result.ok
    assert sandbox_eval.failure_metrics(result)["combined_score"] == 0.0


def test_a_runaway_loop_is_stopped_by_the_wall_clock(tmp_path):
    path = _candidate(tmp_path, '''

def search_algorithm(iterations=1000, bounds=(-5, 5)):
    while True:
        pass


def run_search():
    return search_algorithm()
''')
    _, result = sandbox_eval.evaluate_in_sandbox(EVALUATOR, path, "evaluate", 3.0)
    assert not result.ok
    assert result.duration_s < 15, "the wall clock did not fire"


def test_a_failed_candidate_scores_zero_like_any_other(tmp_path):
    """
    Not a second kind of failure the archive has to understand: upstream
    reports zeroed metrics for an evaluation that raised, and a candidate
    killed for exhausting memory scored nothing either.
    """
    metrics = sandbox_eval.failure_metrics(None)
    assert metrics["combined_score"] == 0.0


# -- failures of the sandbox itself are not verdicts on the candidate ------

def test_an_unavailable_backend_falls_back_rather_than_scoring_everything_zero(
        monkeypatch, tmp_path):
    """
    A sandbox that cannot run is a configuration problem. Reporting zero for
    every candidate would quietly ruin the run and look like the search failing.
    """
    import asyncio

    monkeypatch.setenv(sandbox_eval.ENV_SANDBOX_EVAL, "1")
    monkeypatch.setenv(sandbox_eval.ENV_BACKEND, "does-not-exist")

    from openevolve.evaluator import Evaluator

    original = Evaluator._direct_evaluate
    called = []

    async def fake_original(self, program_path, *a, **kw):
        called.append(program_path)
        return {"combined_score": 0.42}

    Evaluator._direct_evaluate = fake_original
    try:
        assert sandbox_eval.install(None) is True
        wrapped = Evaluator._direct_evaluate

        class FakeEvaluator:
            evaluation_file = EVALUATOR
            config = type("C", (), {"timeout": 10})()

        out = asyncio.run(wrapped(FakeEvaluator(), SEED))
        assert called, "the fallback did not run"
        assert out == {"combined_score": 0.42}
    finally:
        Evaluator._direct_evaluate = original


def test_an_evaluator_with_no_file_falls_back(monkeypatch):
    import asyncio

    monkeypatch.setenv(sandbox_eval.ENV_SANDBOX_EVAL, "1")
    from openevolve.evaluator import Evaluator

    original = Evaluator._direct_evaluate
    called = []

    async def fake_original(self, program_path, *a, **kw):
        called.append(program_path)
        return {"combined_score": 1.0}

    Evaluator._direct_evaluate = fake_original
    try:
        sandbox_eval.install(None)
        wrapped = Evaluator._direct_evaluate

        class NoFile:
            evaluation_file = None
            config = type("C", (), {"timeout": 10})()

        assert asyncio.run(wrapped(NoFile(), SEED)) == {"combined_score": 1.0}
        assert called
    finally:
        Evaluator._direct_evaluate = original
