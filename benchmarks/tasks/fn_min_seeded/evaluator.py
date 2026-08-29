"""Deterministic evaluator for the function-minimization benchmark.

Why this exists
---------------
The upstream example's evaluator runs the candidate ten times and averages. The
candidate draws from an unseeded RNG, so the average is a random variable. On
this repo the *unchanged seed program* scored 1.4184, 1.4431, 1.4060, 1.4229 and
1.0494 across five evaluations -- a range of 0.39 on a metric whose interesting
differences are far smaller than that.

That makes single-run score comparisons meaningless, which in turn blocks every
question worth asking: is the 27B better than the 0.6B, did the prompt change
help, is a config faster *for the same quality*. A run can report "new best
solution found" while the best program is byte-identical to the seed -- observed
here, not hypothesised.

What this changes
-----------------
Trial *i* runs under seed ``SEEDS[i]``, fixed in this file. Two consequences:

* The same program always scores the same number, so a score difference is a
  real difference.
* Two different programs meet the *same ten* random draws, which is a paired
  comparison rather than two independent samples. That removes the draw-to-draw
  variance from the comparison as well, so a much smaller genuine improvement
  becomes visible.

Ten distinct seeds are used rather than one repeated seed, because the point is
still to sample the algorithm's behaviour across starting conditions --
``reliability_score`` and the average over trials only mean something if the
trials differ from each other.

The metric names and weights match the upstream example exactly, so scores are
directly comparable in shape. They are NOT comparable in value: this evaluator
fixes the draws, so the number it reports is one particular sample of the
upstream metric, not an estimate of its mean.

What is pinned, and what is not
-------------------------------
Pinned: ``random``, NumPy's legacy global, and the three NumPy entry points that
silently reach for OS entropy when called with no argument -- ``default_rng()``,
``RandomState()`` and ``PCG64()``. A candidate that writes
``rng = np.random.default_rng()`` is the common idiom and would otherwise defeat
the whole exercise, so it is redirected rather than left as a known hole.

Not pinned, and therefore still able to make a score vary:

* ``secrets``, ``os.urandom``, or a hand-rolled clock seed such as
  ``default_rng(int(time.time()))``. Deliberate reseeding is not something an
  evaluator can prevent.
* Wall-clock-dependent logic -- a candidate that stops after N seconds does
  different amounts of work on a busy machine.
* Set and dict iteration order across processes, via PYTHONHASHSEED. Fixed
  within one process; only the launcher can fix it across processes.
* This evaluator's own ``TRIAL_TIMEOUT_S``. It is wall clock, so a candidate
  whose trials land near five seconds can complete on an idle machine and time
  out on a busy one, and time out on some seeds and not others. It stays
  because the alternative is a candidate with an unbounded loop hanging the run
  for good, and because nothing near the boundary is a program worth keeping --
  the seed's trials are about three milliseconds. Worth knowing before
  concluding that two machines disagree about a slow candidate for an
  interesting reason.

``check_determinism.py`` in this directory measures whether any of that is
actually biting, rather than assuming it is not.
"""

import contextlib
import importlib.util
import random
import time
import traceback

import numpy as np

from openevolve.evaluation_result import EvaluationResult

# Known global minimum (approximate) -- same constants as the upstream example.
GLOBAL_MIN_X = -1.704
GLOBAL_MIN_Y = 0.678
GLOBAL_MIN_VALUE = -1.519

# One seed per trial. Arbitrary but fixed: changing them changes every score
# this benchmark has ever produced, so treat the list as a published constant.
SEEDS = (11, 23, 37, 41, 59, 67, 73, 89, 97, 103)

# The cascade's cheap screen. Three seeds is ~11 ms against ~36 ms, which is
# nothing beside a model call -- the point is to reject a broken candidate
# without running the full set, not to save meaningful time on a good one.
STAGE1_SEEDS = SEEDS[:3]

TRIAL_TIMEOUT_S = 5.0


class _PinnedMeta(type):
    """Metaclass for a stand-in that behaves like the type it replaces.

    A plain function is not good enough. NumPy resolves some of these names out
    of ``np.random`` at call time and hands them to ``isinstance`` -- measured
    on NumPy 2.4.6, ``default_rng(7)`` raises ``TypeError: isinstance() arg 2
    must be a type`` if ``np.random.RandomState`` has been replaced by a
    function. So the replacement has to *be* a type, and has to answer
    ``isinstance`` and ``issubclass`` by delegating to the real one, or the
    patch quietly changes behaviour for anyone who passes a real instance.
    """

    def __instancecheck__(cls, obj):
        return isinstance(obj, cls._real)

    def __subclasscheck__(cls, other):
        return issubclass(other, cls._real)

    def __call__(cls, s=None, *args, **kwargs):
        return cls._real(cls._seed if s is None else s, *args, **kwargs)


def _pinned(real, seed):
    return _PinnedMeta(real.__name__, (), {"_real": real, "_seed": seed})


@contextlib.contextmanager
def pinned_randomness(seed):
    """Run the body with every reachable RNG pinned to ``seed``.

    Restores the previous global state on the way out, so importing this
    evaluator cannot quietly derandomise the process that called it.
    """
    py_state = random.getstate()
    np_state = np.random.get_state()
    originals = {
        name: getattr(np.random, name)
        for name in ("default_rng", "RandomState", "PCG64")
    }

    random.seed(seed)
    np.random.seed(seed)
    # default_rng is a function, so a plain wrapper is fine and cheaper; the
    # other two are types that NumPy itself passes to isinstance.
    real_default_rng = originals["default_rng"]
    np.random.default_rng = lambda s=None: real_default_rng(seed if s is None else s)
    np.random.RandomState = _pinned(originals["RandomState"], seed)
    np.random.PCG64 = _pinned(originals["PCG64"], seed)
    try:
        yield
    finally:
        for name, original in originals.items():
            setattr(np.random, name, original)
        random.setstate(py_state)
        np.random.set_state(np_state)


def _load(program_path, seed):
    """Import the candidate with randomness already pinned.

    Module-level RNG construction -- ``rng = np.random.default_rng()`` at import
    time -- happens during ``exec_module``, so the patches have to be in place
    before the import, not merely before the call.
    """
    spec = importlib.util.spec_from_file_location("program", program_path)
    program = importlib.util.module_from_spec(spec)
    with pinned_randomness(seed):
        spec.loader.exec_module(program)
    return program


def _run_with_timeout(func, timeout_seconds=None):
    """Call ``func`` on a worker thread, giving up after ``timeout_seconds``.

    Defaults to ``TRIAL_TIMEOUT_S`` read at call time rather than bound into the
    signature, so a caller -- a test, mostly -- can lower it without patching
    this function. A default argument would freeze the value at import.

    A thread is used rather than a process because the pinned RNG state lives in
    this interpreter -- a subprocess would not inherit the patches, and would
    also pay 2.9 s to import the engine, eighty times what an evaluation costs.

    A bare daemon thread rather than ``ThreadPoolExecutor``, for two reasons
    that both cost real time to find:

    * ``with ThreadPoolExecutor(...)`` calls ``shutdown(wait=True)`` on exit and
      blocks until the runaway thread finishes. The timeout raises on schedule
      and then the call sits there anyway -- measured before this was fixed, a
      12 s function under a 5 s timeout returned after 12.0 s.
    * Its threads are non-daemon and joined by an ``atexit`` hook, so even with
      ``shutdown(wait=False)`` the *interpreter* will not exit while a runaway
      trial is alive. Evaluating a candidate with a huge search budget left a
      process that had printed its results and could not terminate.

    Python cannot kill a thread, so a timed-out trial does keep burning CPU
    until it finishes on its own. What this bounds is how long the evaluation
    waits for it -- a runaway candidate costs a fixed 5 s per trial instead of
    however long it likes -- and that the process can still exit.
    """
    import threading

    if timeout_seconds is None:
        timeout_seconds = TRIAL_TIMEOUT_S

    outcome = {}

    def run():
        try:
            outcome["value"] = func()
        except BaseException as exc:  # reported on the calling thread instead
            outcome["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout_seconds)

    if worker.is_alive():
        raise TimeoutError("trial exceeded {}s".format(timeout_seconds))
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def _unpack(result):
    """Accept (x, y) or (x, y, value); compute the value if it was omitted."""
    if not isinstance(result, tuple):
        raise ValueError(
            "run_search returned {}, expected a tuple".format(type(result).__name__)
        )
    if len(result) == 3:
        x, y, value = result
    elif len(result) == 2:
        x, y = result
        value = np.sin(x) * np.cos(y) + np.sin(x * y) + (x**2 + y**2) / 20
    else:
        raise ValueError(
            "run_search returned {} values, expected 2 or 3".format(len(result))
        )
    x, y, value = float(x), float(y), float(value)
    if not all(np.isfinite(v) for v in (x, y, value)):
        raise ValueError(
            "run_search returned non-finite values: ({}, {}, {})".format(x, y, value)
        )
    return x, y, value


def _zero(reason, **artifacts):
    return EvaluationResult(
        metrics={
            "value_score": 0.0,
            "distance_score": 0.0,
            "reliability_score": 0.0,
            "combined_score": 0.0,
            "error": reason,
        },
        artifacts=artifacts,
    )


def evaluate(program_path):
    """Score the program at ``program_path``. Same input twice gives the same output."""
    return _evaluate_with(program_path, SEEDS)


def evaluate_stage1(program_path):
    """Cheap screen: the first few seeds only.

    The engine's cascade runs this first and only proceeds past
    ``cascade_thresholds[0]`` (0.5 by default), so it has to report
    ``combined_score`` on the same scale as the full evaluation -- a gate metric
    alone would be compared against that threshold and reject everything.

    A subset score is not the score, and does not need to be: ``_cascade_evaluate``
    merges stage 2's metrics *over* stage 1's, so a candidate that gets past this
    gate is reported with the full ten-seed number and is directly comparable to
    a run with cascade switched off. ``test_cascade_score_matches_direct_score``
    is what holds that.

    A candidate that *fails* the gate is reported with this subset score instead,
    which for a broken program is 0.0 either way but need not match the ten-seed
    number in general. That is what a cascade is for, and it only affects
    candidates already being rejected -- but it does mean "cascade agrees with
    direct" is a claim about accepted candidates, not about every candidate.
    """
    return _evaluate_with(program_path, STAGE1_SEEDS)


def evaluate_stage2(program_path):
    """The real evaluation, over all ten seeds."""
    return _evaluate_with(program_path, SEEDS)


def _evaluate_with(program_path, seeds):
    try:
        probe = _load(program_path, seeds[0])
        if not hasattr(probe, "run_search"):
            return _zero(
                "Missing run_search function",
                error_type="MissingFunction",
                error_message="Program is missing required 'run_search' function",
                suggestion=(
                    "Define a function named 'run_search' that returns (x, y, value) "
                    "or (x, y)"
                ),
            )

        values, distances, times = [], [], []
        last_x = last_y = None
        failures = []

        for seed in seeds:
            # Reload per trial so a module-level RNG is rebuilt under this
            # trial's seed. Without it every trial after the first would share
            # the first trial's generator, and the ten trials would collapse
            # into one sample repeated ten times.
            try:
                program = _load(program_path, seed)
                with pinned_randomness(seed):
                    started = time.perf_counter()
                    x, y, value = _unpack(_run_with_timeout(program.run_search))
                    elapsed = time.perf_counter() - started
            except Exception as exc:
                failures.append(
                    "seed {}: {}: {}".format(seed, type(exc).__name__, exc)
                )
                continue

            values.append(value)
            distances.append(float(np.hypot(x - GLOBAL_MIN_X, y - GLOBAL_MIN_Y)))
            times.append(elapsed)
            last_x, last_y = x, y

        if not values:
            return _zero(
                "All trials failed",
                error_type="AllTrialsFailed",
                error_message="All {} trials failed".format(len(seeds)),
                failures="\n".join(failures),
                suggestion=(
                    "Check for infinite loops, ensure run_search returns (x, y) or "
                    "(x, y, value), and verify the algorithm finishes within "
                    "{}s".format(TRIAL_TIMEOUT_S)
                ),
            )

        avg_value = float(np.mean(values))
        avg_distance = float(np.mean(distances))

        value_score = float(1.0 / (1.0 + abs(avg_value - GLOBAL_MIN_VALUE)))
        distance_score = float(1.0 / (1.0 + avg_distance))
        reliability_score = float(len(values) / len(seeds))

        if avg_distance < 0.5:
            quality = 1.5
        elif avg_distance < 1.5:
            quality = 1.2
        elif avg_distance < 3.0:
            quality = 1.0
        else:
            quality = 0.7

        base = 0.5 * value_score + 0.3 * distance_score + 0.2 * reliability_score
        combined_score = float(base * quality)

        artifacts = {
            "trials": "{} of {} seeds succeeded".format(len(values), len(seeds)),
            "best_position": "x={:.4f}, y={:.4f}".format(last_x, last_y),
            "average_distance_to_global": "{:.4f}".format(avg_distance),
            "average_value": "{:.4f}".format(avg_value),
            # Wall clock is reported but deliberately not scored: it is the one
            # quantity here that is not reproducible, and folding it into
            # combined_score would put the noise straight back in.
            "average_seconds": "{:.4f}".format(float(np.mean(times))),
        }
        if failures:
            artifacts["failures"] = "\n".join(failures)

        return EvaluationResult(
            metrics={
                "value_score": value_score,
                "distance_score": distance_score,
                "reliability_score": reliability_score,
                "combined_score": combined_score,
            },
            artifacts=artifacts,
        )

    except Exception as exc:
        return _zero(
            str(exc),
            error_type=type(exc).__name__,
            error_message=str(exc),
            full_traceback=traceback.format_exc(),
            suggestion="Check for syntax errors or missing imports in the generated code",
        )
