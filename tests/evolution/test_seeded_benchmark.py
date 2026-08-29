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
