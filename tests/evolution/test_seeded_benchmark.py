"""The deterministic benchmark's guarantee, tested rather than asserted.

`benchmarks/tasks/fn_min_seeded` exists so that a score difference between two
programs means a difference between the programs. Everything here fails if that
stops being true -- most importantly `test_identical_scores_across_evaluations`,
which is the whole claim in one assertion.
"""

import importlib.util
import random
import sys
import textwrap
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
TASK = ROOT / "benchmarks" / "tasks" / "fn_min_seeded"


@pytest.fixture(scope="module")
def ev():
    """The evaluator, loaded by path -- benchmarks/ is not an importable package."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(
        "fn_min_seeded_evaluator", TASK / "evaluator.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(tmp_path, body, name="candidate.py"):
    path = tmp_path / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return str(path)


# --------------------------------------------------------------------------
# The guarantee
# --------------------------------------------------------------------------


def test_identical_scores_across_evaluations(ev, tmp_path):
    """The same program scores exactly the same number every time.

    Exactly, not approximately: the reason this task exists is that a tolerance
    is indistinguishable from the noise it was built to remove.
    """
    program = write(
        tmp_path,
        """
        import numpy as np

        def run_search():
            best = (0.0, 0.0, float("inf"))
            for _ in range(200):
                x = np.random.uniform(-5, 5)
                y = np.random.uniform(-5, 5)
                v = np.sin(x) * np.cos(y) + np.sin(x * y) + (x**2 + y**2) / 20
                if v < best[2]:
                    best = (x, y, v)
            return best
        """,
    )
    scores = {ev.evaluate(program).metrics["combined_score"] for _ in range(4)}
    assert len(scores) == 1, "scores drifted across evaluations: {}".format(scores)


def test_seed_program_is_deterministic(ev):
    """The shipped seed program specifically, since every run starts from it."""
    seed_program = str(TASK / "initial_program.py")
    first = ev.evaluate(seed_program).metrics["combined_score"]
    second = ev.evaluate(seed_program).metrics["combined_score"]
    assert first == second


def test_trials_are_not_all_the_same_draw(ev, tmp_path):
    """Ten seeds must produce ten different trials.

    Pinning the RNG to one value for the whole evaluation would also be
    deterministic, and would silently reduce the benchmark to a single sample
    reported ten times -- making reliability_score meaningless and the average
    a lie. The distinct seeds are load-bearing.
    """
    program = write(
        tmp_path,
        """
        import numpy as np

        def run_search():
            x = np.random.uniform(-5, 5)
            y = np.random.uniform(-5, 5)
            return x, y
        """,
    )
    seen = set()
    for seed in ev.SEEDS:
        module = ev._load(program, seed)
        with ev.pinned_randomness(seed):
            seen.add(module.run_search())
    assert len(seen) == len(ev.SEEDS)


# --------------------------------------------------------------------------
# The entropy holes the evaluator claims to close
# --------------------------------------------------------------------------


@pytest.mark.parametrize("factory", ["default_rng", "RandomState", "PCG64"])
def test_unseeded_numpy_factories_are_pinned(ev, factory):
    """`np.random.default_rng()` and friends must not reach for OS entropy.

    Called with no argument these bypass the global seed entirely, so a
    candidate using the modern idiom would defeat the whole exercise.
    """
    with ev.pinned_randomness(1234):
        a = np.random.Generator(getattr(np.random, factory)()) \
            if factory == "PCG64" else getattr(np.random, factory)()
        first = a.uniform(size=3) if hasattr(a, "uniform") else None
    with ev.pinned_randomness(1234):
        b = np.random.Generator(getattr(np.random, factory)()) \
            if factory == "PCG64" else getattr(np.random, factory)()
        second = b.uniform(size=3) if hasattr(b, "uniform") else None
    assert np.array_equal(first, second)


def test_explicit_seed_is_still_honoured(ev):
    """Pinning must redirect only the *unseeded* call, not override a real argument."""
    with ev.pinned_randomness(1234):
        explicit = np.random.default_rng(7).uniform(size=3)
    expected = np.random.default_rng(7).uniform(size=3)
    assert np.array_equal(explicit, expected)


def test_module_level_rng_is_rebuilt_per_trial(ev, tmp_path):
    """A generator built at import time must still differ between trials.

    The patches have to be in place before `exec_module`, and the module has to
    be re-imported per trial -- otherwise every trial shares the first trial's
    generator.
    """
    program = write(
        tmp_path,
        """
        import numpy as np

        RNG = np.random.default_rng()

        def run_search():
            return RNG.uniform(-5, 5), RNG.uniform(-5, 5)
        """,
    )
    seen = set()
    for seed in ev.SEEDS:
        module = ev._load(program, seed)
        with ev.pinned_randomness(seed):
            seen.add(module.run_search())
    assert len(seen) == len(ev.SEEDS)


def test_global_rng_state_is_restored(ev):
    """Importing or running the evaluator must not derandomise the caller.

    A benchmark that quietly seeds the host process would make every *other*
    measurement in the same process reproducible-looking and wrong.
    """
    random.seed(999)
    np.random.seed(999)
    py_before = random.random()
    np_before = np.random.random()

    random.seed(999)
    np.random.seed(999)
    with ev.pinned_randomness(1):
        random.random()
        np.random.random()
    assert random.random() == py_before
    assert np.random.random() == np_before


def test_factories_are_restored(ev):
    """The monkeypatches must come back off, including when the body raises."""
    original = np.random.default_rng
    with pytest.raises(RuntimeError):
        with ev.pinned_randomness(5):
            assert np.random.default_rng is not original
            raise RuntimeError("boom")
    assert np.random.default_rng is original


# --------------------------------------------------------------------------
# Contract with the engine
# --------------------------------------------------------------------------


def test_metric_names_match_upstream(ev):
    """Same metric names as the upstream example, so configs and UI need no fork."""
    metrics = ev.evaluate(str(TASK / "initial_program.py")).metrics
    assert set(metrics) == {
        "value_score",
        "distance_score",
        "reliability_score",
        "combined_score",
    }
    assert all(isinstance(v, float) for v in metrics.values())


def test_missing_run_search_scores_zero(ev, tmp_path):
    program = write(tmp_path, "x = 1\n")
    result = ev.evaluate(program)
    assert result.metrics["combined_score"] == 0.0
    assert result.artifacts["error_type"] == "MissingFunction"


def test_all_trials_failing_scores_zero(ev, tmp_path):
    program = write(
        tmp_path,
        """
        def run_search():
            raise ValueError("no")
        """,
    )
    result = ev.evaluate(program)
    assert result.metrics["combined_score"] == 0.0
    assert result.artifacts["error_type"] == "AllTrialsFailed"
    assert "ValueError" in result.artifacts["failures"]


def test_import_error_scores_zero_rather_than_raising(ev, tmp_path):
    """A candidate that will not import is a bad score, not a crashed run."""
    program = write(tmp_path, "this is not python\n")
    result = ev.evaluate(program)
    assert result.metrics["combined_score"] == 0.0
    assert "error" in result.metrics


def test_two_tuple_return_is_accepted(ev, tmp_path):
    """Upstream accepts (x, y) and computes the value; so must this."""
    program = write(
        tmp_path,
        """
        def run_search():
            return -1.704, 0.678
        """,
    )
    result = ev.evaluate(program)
    assert result.metrics["combined_score"] > 0.0
    assert result.metrics["reliability_score"] == 1.0


def test_non_finite_result_is_a_failed_trial(ev, tmp_path):
    """NaN must not propagate into the metrics as a plausible-looking score."""
    program = write(
        tmp_path,
        """
        def run_search():
            return float("nan"), 0.0, 0.0
        """,
    )
    result = ev.evaluate(program)
    assert result.metrics["combined_score"] == 0.0
    assert result.artifacts["error_type"] == "AllTrialsFailed"


def test_partial_failure_lowers_reliability_but_still_scores(ev, tmp_path):
    """Some trials failing is a worse score, not a zero."""
    program = write(
        tmp_path,
        """
        CALLS = []

        def run_search():
            CALLS.append(1)
            # The module is re-imported per trial, so CALLS never grows past 1;
            # fail on a property of the seed instead.
            import random
            if random.random() < 0.5:
                raise ValueError("unlucky")
            return -1.704, 0.678
        """,
    )
    result = ev.evaluate(program)
    reliability = result.metrics["reliability_score"]
    assert 0.0 < reliability < 1.0
    assert result.metrics["combined_score"] > 0.0
    # And the partial failure is reproducible like everything else.
    assert ev.evaluate(program).metrics["reliability_score"] == reliability


def test_wall_clock_is_reported_but_not_scored(ev):
    """Timing is the one irreproducible quantity; it must stay out of the score."""
    result = ev.evaluate(str(TASK / "initial_program.py"))
    assert "average_seconds" in result.artifacts
    assert not any("time" in name or "speed" in name for name in result.metrics)


# --------------------------------------------------------------------------
# The documented finding
# --------------------------------------------------------------------------


def test_refined_program_wins_most_seeds_but_scores_lower(ev):
    """The README's central claim, pinned so it cannot rot.

    `refined_program.py` is closer to the global minimum on 9 of 10 seeds and
    still scores lower, because combined_score averages distances and one
    trapped trial outweighs nine good ones. If this ever stops being true the
    README is wrong and needs rewriting -- it is a documented property of the
    metric, not a bug to fix here.
    """
    seed_program = str(TASK / "initial_program.py")
    refined = str(TASK / "refined_program.py")

    def distances(path):
        out = []
        for seed in ev.SEEDS:
            module = ev._load(path, seed)
            with ev.pinned_randomness(seed):
                x, y, _ = ev._unpack(module.run_search())
            out.append(float(np.hypot(x - ev.GLOBAL_MIN_X, y - ev.GLOBAL_MIN_Y)))
        return out

    base, refined_d = distances(seed_program), distances(refined)
    wins = sum(r < b for b, r in zip(base, refined_d))

    assert wins >= 9, "refined program won {} seeds, README says 9".format(wins)
    assert max(refined_d) > 3.0, "the outlier trial that drives the finding is gone"
    assert (
        ev.evaluate(refined).metrics["combined_score"]
        < ev.evaluate(seed_program).metrics["combined_score"]
    )


def test_patched_types_answer_isinstance_like_the_real_ones(ev):
    """The stand-ins must delegate isinstance, not merely be callable.

    NumPy resolves `np.random.RandomState` at call time and hands it to
    isinstance -- on NumPy 2.4.6 a plain function there makes `default_rng(7)`
    raise "isinstance() arg 2 must be a type". Anything that keeps the patch a
    non-type, or drops the delegation, breaks real candidates.
    """
    real_instance = np.random.RandomState(3)
    real_bitgen = np.random.PCG64(3)
    with ev.pinned_randomness(99):
        assert isinstance(np.random.RandomState, type)
        assert isinstance(real_instance, np.random.RandomState)
        assert isinstance(real_bitgen, np.random.PCG64)
        assert issubclass(type(real_instance), np.random.RandomState)
        # The check that actually crashed before the fix.
        np.random.default_rng(7)


def test_score_can_be_bought_with_compute(ev, tmp_path):
    """Raising the search budget raises the score, with no better algorithm.

    A documented property of upstream's metric, pinned here because it changes
    how results from this task must be read. Nothing scores runtime, so the
    cheapest available improvement is "do more sampling" -- and that is exactly
    what the first real run found: a 0.6B model's only accepted change in 30
    iterations was `iterations=1000` -> `iterations=2000`.

    The consequence is that a score rise on this task is evidence the loop
    works, and is NOT by itself evidence that the model can improve an
    algorithm. Read `average_seconds` in the artifacts alongside the score.

    Not fixed here: reweighting would make every number already recorded
    against this task incomparable, and the same property is true of
    `examples/function_minimization`.
    """
    seed_source = (TASK / "initial_program.py").read_text(encoding="utf-8")
    assert "iterations=1000" in seed_source

    def score_with(budget):
        path = tmp_path / "budget_{}.py".format(budget)
        path.write_text(
            seed_source.replace("iterations=1000", "iterations={}".format(budget)),
            encoding="utf-8",
        )
        return ev.evaluate(str(path)).metrics["combined_score"]

    assert score_with(2000) > score_with(1000) > score_with(500)


# --------------------------------------------------------------------------
# Cascade evaluation
# --------------------------------------------------------------------------


def test_cascade_functions_exist(ev):
    """The shipped local config enables cascade; without these it warns and
    silently falls back, making the setting useless."""
    assert hasattr(ev, "evaluate_stage1")
    assert hasattr(ev, "evaluate_stage2")


def test_cascade_score_matches_direct_score(ev):
    """A cascade run and a direct run must report the same number.

    The engine merges stage 2's metrics *over* stage 1's, so stage 1 being a
    cheaper subset is invisible in the result. If that ever stops holding, two
    runs of the same program become incomparable depending on a config flag --
    exactly the disease this whole task was built to cure.
    """
    program = str(TASK / "initial_program.py")
    direct = ev.evaluate(program).metrics

    # Reproduce _cascade_evaluate's merge: stage 1, then stage 2 over the top.
    merged = {}
    for stage in (ev.evaluate_stage1(program), ev.evaluate_stage2(program)):
        merged.update(
            {k: float(v) for k, v in stage.metrics.items() if k != "error"}
        )

    assert merged["combined_score"] == direct["combined_score"]


def test_stage1_passes_the_default_cascade_threshold(ev):
    """Stage 1 must report combined_score on the full scale, not a gate flag.

    `_passes_threshold` compares `combined_score` against `cascade_thresholds[0]`
    (0.5 by default). A stage that returned only a pass/fail metric, or a score
    on a different scale, would gate out working programs before stage 2 ever
    ran.
    """
    score = ev.evaluate_stage1(str(TASK / "initial_program.py")).metrics
    assert score["combined_score"] >= 0.5
    assert set(score) == {
        "value_score",
        "distance_score",
        "reliability_score",
        "combined_score",
    }


def test_stage1_rejects_a_broken_program(ev, tmp_path):
    """A broken candidate must fail the gate rather than reach stage 2."""
    program = write(
        tmp_path,
        """
        def run_search():
            raise ValueError("no")
        """,
    )
    assert ev.evaluate_stage1(program).metrics["combined_score"] < 0.5


def test_stage1_is_cheaper_than_the_full_evaluation(ev):
    """Otherwise the cascade is pure overhead."""
    assert len(ev.STAGE1_SEEDS) < len(ev.SEEDS)
    assert tuple(ev.STAGE1_SEEDS) == tuple(ev.SEEDS[: len(ev.STAGE1_SEEDS)])


def test_stage1_is_deterministic_too(ev):
    program = str(TASK / "initial_program.py")
    assert (
        ev.evaluate_stage1(program).metrics["combined_score"]
        == ev.evaluate_stage1(program).metrics["combined_score"]
    )


def test_gate_failure_reports_the_subset_score(ev, tmp_path):
    """Below the gate, cascade reports stage 1's number rather than the full one.

    That is what a cascade is for, and it only touches candidates already being
    rejected -- but it means "cascade agrees with direct" is a claim about
    accepted candidates. Pinned so the caveat in the README stays true.
    """
    program = write(
        tmp_path,
        """
        import random

        def run_search():
            if random.random() < 0.5:
                raise ValueError("unlucky")
            return 4.9, 4.9
        """,
    )
    stage1 = ev.evaluate_stage1(program).metrics["combined_score"]
    full = ev.evaluate(program).metrics["combined_score"]
    assert stage1 < 0.5, "this fixture is meant to fail the gate"
    # Both are reproducible; they simply need not be equal.
    assert stage1 == ev.evaluate_stage1(program).metrics["combined_score"]
    assert full == ev.evaluate(program).metrics["combined_score"]


# --------------------------------------------------------------------------
# The trial timeout
# --------------------------------------------------------------------------


def test_the_timeout_actually_gives_up_on_time(ev):
    """It has to bound the *wait*, not just raise on schedule.

    Written with `with ThreadPoolExecutor(...)`, __exit__ calls
    shutdown(wait=True) and blocks until the runaway thread finishes -- so the
    TimeoutError fires at 5s and the call still returns at 12s. Measured before
    the fix: 12.0s under a 5s limit.
    """
    import time

    started = time.perf_counter()
    with pytest.raises(TimeoutError):
        ev._run_with_timeout(lambda: time.sleep(12), timeout_seconds=1.0)
    elapsed = time.perf_counter() - started

    assert elapsed < 5.0, f"waited {elapsed:.1f}s for a 1s timeout"


def test_the_worker_thread_cannot_block_interpreter_exit(ev):
    """A runaway trial must not leave a process that cannot terminate.

    ThreadPoolExecutor's threads are non-daemon and joined by an atexit hook, so
    even shutdown(wait=False) leaves the interpreter waiting. Evaluating a
    candidate with a huge search budget produced a process that printed its
    results and then hung.
    """
    import subprocess
    import sys

    child = subprocess.run(
        [sys.executable, "-c",
         "import importlib.util, sys, time;"
         "sys.path.insert(0, '.');"
         "s = importlib.util.spec_from_file_location("
         "'ev', 'benchmarks/tasks/fn_min_seeded/evaluator.py');"
         "m = importlib.util.module_from_spec(s); s.loader.exec_module(m);"
         "\ntry:\n    m._run_with_timeout(lambda: time.sleep(60), timeout_seconds=1.0)"
         "\nexcept TimeoutError:\n    pass\nprint('done')"],
        capture_output=True, text=True, timeout=45,
    )
    assert child.returncode == 0, child.stderr
    assert "done" in child.stdout


def test_a_timed_out_trial_does_not_take_the_evaluation_with_it(ev, tmp_path, monkeypatch):
    """A candidate that hangs scores zero rather than hanging the run.

    The real limit is 5s, so ten seeds would make this a 50s test. The value is
    read at call time precisely so it can be lowered here -- what is under test
    is that a timeout is survivable, not how long it is.
    """
    monkeypatch.setattr(ev, "TRIAL_TIMEOUT_S", 0.3)
    program = write(
        tmp_path,
        """
        import time

        def run_search():
            time.sleep(30)
            return 0.0, 0.0
        """,
    )
    result = ev.evaluate(program)
    assert result.metrics["combined_score"] == 0.0
    assert result.artifacts["error_type"] == "AllTrialsFailed"
    assert "TimeoutError" in result.artifacts["failures"]


def test_an_exception_is_not_disguised_as_a_timeout(ev):
    """The worker runs on another thread; its exception has to be re-raised on
    the caller's, or a broken candidate would be reported as a slow one."""
    def boom():
        raise ValueError("kaboom")

    with pytest.raises(ValueError, match="kaboom"):
        ev._run_with_timeout(boom)


def test_the_normal_path_is_untouched(ev):
    assert ev._run_with_timeout(lambda: (1.0, 2.0, 3.0)) == (1.0, 2.0, 3.0)
