"""
V1 verification: deciding whether an improvement is real.

The evaluator answers "what did this score?", which is not "is this score
honest". Evolutionary search is unusually good at finding the gap, so the tests
that matter are the ones that feed it programs which *cheat* and check they are
caught — not the ones that feed it correct programs and check they pass.

Four cheats, each a real way to beat this task's evaluator:
report a value you never computed; ignore the bounds you were given; return
NaN, which compares favourably against every real number; and score a lucky
draw rather than an improvement.
"""

import math

import pytest

from oe_max.verification import (
    Counterexample, CounterexampleStore, SuspicionDetector, V1Verifier,
    VerificationSpec, load_spec,
)

EVALUATOR = "examples/function_minimization/evaluator.py"

OBJECTIVE = '''
import numpy as np
def evaluate_function(x, y):
    return np.sin(x)*np.cos(y) + np.sin(x*y) + (x**2 + y**2)/20
'''

HONEST = OBJECTIVE + '''
def search_algorithm(iterations=200, bounds=(-5, 5)):
    import numpy as np
    best = None
    for _ in range(iterations):
        x = np.random.uniform(bounds[0], bounds[1])
        y = np.random.uniform(bounds[0], bounds[1])
        v = evaluate_function(x, y)
        if best is None or v < best[2]:
            best = (x, y, v)
    return best
'''

FABRICATED_VALUE = OBJECTIVE + '''
def search_algorithm(iterations=1000, bounds=(-5, 5)):
    return 0.0, 0.0, -999.0
'''

IGNORES_BOUNDS = OBJECTIVE + '''
def search_algorithm(iterations=1000, bounds=(-5, 5)):
    x, y = -1.7047, 0.8720
    return x, y, evaluate_function(x, y)
'''

RETURNS_NAN = OBJECTIVE + '''
def search_algorithm(iterations=1000, bounds=(-5, 5)):
    return float("nan"), float("nan"), float("-inf")
'''

NON_DETERMINISTIC = OBJECTIVE + '''
import time
def search_algorithm(iterations=1000, bounds=(-5, 5)):
    import numpy as np
    rng = np.random.RandomState(int(time.time_ns() % 100000))
    x, y = rng.uniform(*bounds), rng.uniform(*bounds)
    return x, y, evaluate_function(x, y)
'''


@pytest.fixture
def spec():
    return load_spec(EVALUATOR)


@pytest.fixture
def verifier(spec):
    return V1Verifier(spec, CounterexampleStore(), entry_point="search_algorithm")


# -- the spec loads ---------------------------------------------------------

def test_the_task_declares_checks_of_every_kind(spec):
    counts = spec.summary()["counts"]
    assert spec.declared
    assert counts["property"] >= 3 and counts["metamorphic"] >= 1
    assert counts["randomized"] >= 1 and spec.generator is not None


def test_a_task_with_no_spec_says_so_rather_than_claiming_success(tmp_path):
    """
    "Verified" for a task that declared nothing would be worse than not
    verifying: it is a claim of safety nobody made.
    """
    spec = load_spec(str(tmp_path / "evaluator.py"))
    assert spec.declared is False and spec.checks == []

    report = V1Verifier(spec, entry_point="search_algorithm").verify(HONEST, "c1")
    assert report.passed
    assert report.spec_declared is False
    assert "declares no verification of its own" in report.summary()


# -- honest programs pass ---------------------------------------------------

def test_the_shipped_seed_program_passes(verifier):
    code = open("examples/function_minimization/initial_program.py").read()
    report = verifier.verify(code, "seed")
    assert report.passed, report.summary()
    assert report.spec_declared


def test_an_honest_search_passes(verifier):
    assert verifier.verify(HONEST, "honest").passed


# -- cheats are caught ------------------------------------------------------

def test_a_fabricated_value_is_caught(verifier):
    """The cheapest cheat: return a low number and any point at all."""
    report = verifier.verify(FABRICATED_VALUE, "cheat")
    assert not report.passed
    assert any(f.name == "reported_value_matches_the_point" for f in report.failures)


def test_hard_coding_the_answer_is_caught_by_the_metamorphic_check(verifier):
    """
    The one a single run cannot catch. This program satisfies every
    single-run property — the point is in bounds, the value is real and
    correctly computed — and is still not searching.
    """
    report = verifier.verify(IGNORES_BOUNDS, "hardcoded")
    assert not report.passed
    kinds = {f.kind for f in report.failures}
    assert "metamorphic" in kinds or "randomized" in kinds
    # Every property-level check passes, which is the whole point.
    assert not [f for f in report.failures if f.kind == "property"]


def test_nan_is_caught(verifier):
    """NaN compares favourably against every real number, so it wins by default."""
    report = verifier.verify(RETURNS_NAN, "nan")
    assert not report.passed


def test_a_score_that_is_a_lucky_draw_is_caught(verifier):
    """
    Not a cheat by intent, and the most costly one to miss: an archive that
    keeps the best single draw keeps the lucky one, and every later generation
    builds on noise. An evaluator that runs once cannot see it.
    """
    report = verifier.verify(NON_DETERMINISTIC, "noisy")
    assert not report.passed
    assert any(f.name == "deterministic" for f in report.failures)


def test_a_candidate_that_does_not_load_fails_at_the_first_check(verifier):
    report = verifier.verify("def broken(:\n    pass", "syntax")
    assert not report.passed
    assert report.results[0].name == "importable"
    assert len(report.results) == 1, "nothing should run against a module that failed to load"


# -- our own bugs are not the candidate's fault -----------------------------

def test_a_check_that_raises_is_reported_not_charged_to_the_candidate():
    """
    Rejecting a program because *our* test blew up would quietly delete good
    work. The candidate still passes; the broken check is reported loudly.
    """
    from oe_max.verification.spec import PROPERTY, Check

    def exploding(module):
        raise RuntimeError("the check itself is broken")

    spec = VerificationSpec(
        checks=[Check("exploding", PROPERTY, exploding)], declared=True)
    report = V1Verifier(spec, entry_point="search_algorithm").verify(HONEST, "c1")

    assert report.passed
    assert len(report.errors) == 1
    assert not report.failures


def test_a_check_may_assert_return_a_bool_or_return_a_reason():
    """Checks written entirely with asserts must work without ceremony."""
    from oe_max.verification.spec import PROPERTY, Check

    spec = VerificationSpec(declared=True, checks=[
        Check("asserts", PROPERTY, lambda m: None),
        Check("returns_true", PROPERTY, lambda m: True),
        Check("returns_false", PROPERTY, lambda m: False),
        Check("returns_reason", PROPERTY, lambda m: (False, "because of X")),
    ])
    report = V1Verifier(spec, entry_point="search_algorithm").verify(HONEST, "c1")

    failed = {f.name: f.message for f in report.failures}
    assert set(failed) == {"returns_false", "returns_reason"}
    assert failed["returns_reason"] == "because of X"


# -- counterexamples --------------------------------------------------------

def test_every_failure_becomes_a_counterexample(verifier):
    report = verifier.verify(FABRICATED_VALUE, "cheat")
    assert len(report.counterexamples) == len(report.failures)
    assert len(verifier.store) == len(report.failures)


def test_the_caller_s_store_is_the_one_that_gets_filled(spec):
    """
    The bug this pins: CounterexampleStore defines __len__, so an empty store
    is falsy and `store or CounterexampleStore()` replaced the caller's store
    with a fresh one. Failures were detected correctly and the evidence was
    thrown away into an object nobody held.
    """
    store = CounterexampleStore()
    V1Verifier(spec, store, entry_point="search_algorithm").verify(
        FABRICATED_VALUE, "cheat")
    assert len(store) > 0


def test_counterexamples_reach_a_prompt_in_a_usable_form(verifier):
    verifier.verify(IGNORES_BOUNDS, "hardcoded")
    context = verifier.store.prompt_context(3)
    assert "must not repeat" in context
    assert "bounds" in context


def test_an_empty_store_offers_no_failure_context():
    """
    An operator gated on `has_failure` must not be offered an empty one — the
    request would be vague and the operator would take the blame.
    """
    assert CounterexampleStore().prompt_context() == ""


# -- the counterexample store itself ----------------------------------------

def _ce(check="property:bounds", inputs=None, **kw):
    return Counterexample(check=check, inputs=inputs or {"bounds": (0, 1)}, **kw)


def test_the_same_failure_twice_is_one_piece_of_evidence():
    """Otherwise the store fills with the easiest failure to make."""
    store = CounterexampleStore()
    store.add(_ce())
    store.add(_ce())
    assert len(store) == 1
    assert store.most_valuable()[0].hits == 2


def test_identity_is_the_check_and_the_input_not_the_message():
    store = CounterexampleStore()
    store.add(_ce(message="x=3 escaped"))
    store.add(_ce(message="x=4 escaped"))
    assert len(store) == 1


def test_a_different_input_is_different_evidence():
    store = CounterexampleStore()
    store.add(_ce(inputs={"bounds": (0, 1)}))
    store.add(_ce(inputs={"bounds": (2, 3)}))
    assert len(store) == 2


def test_eviction_keeps_what_keeps_catching_candidates():
    """
    A counterexample that has caught six candidates describes a mistake the
    search keeps making. It outranks one that caught a single candidate later.
    """
    store = CounterexampleStore(capacity=2)
    repeat = store.add(_ce(inputs={"i": 0}))
    for _ in range(5):
        store.add(_ce(inputs={"i": 0}))
    store.add(_ce(inputs={"i": 1}))
    store.add(_ce(inputs={"i": 2}))

    assert len(store) == 2
    assert repeat.fingerprint() in {c.fingerprint() for c in store.most_valuable(2)}


def test_the_store_survives_a_restart(tmp_path):
    path = str(tmp_path / "ce.json")
    store = CounterexampleStore(path)
    store.add(_ce(message="x escaped"))
    store.add(_ce(message="x escaped"))
    store.save()

    revived = CounterexampleStore(path)
    assert len(revived) == 1
    assert revived.most_valuable()[0].hits == 2


def test_a_corrupt_store_reads_as_empty_rather_than_raising(tmp_path):
    """Evidence, not correctness input: an unreadable store must not stop a run."""
    path = tmp_path / "ce.json"
    path.write_text("{ this is not json")
    assert len(CounterexampleStore(str(path))) == 0


def test_saving_is_atomic(tmp_path):
    """A run killed mid-write must not leave a truncated store reading as empty."""
    path = str(tmp_path / "ce.json")
    store = CounterexampleStore(path)
    store.add(_ce())
    store.save()
    assert not (tmp_path / "ce.json.tmp").exists()
    assert len(CounterexampleStore(path)) == 1


# -- suspicion --------------------------------------------------------------

def test_no_history_means_no_verdict():
    """"Unusual" is not a meaningful word about three observations."""
    d = SuspicionDetector()
    v = d.check(10.0)
    assert not v.suspicious
    assert "not enough history" in v.reason


def test_a_jump_far_beyond_the_run_s_own_history_is_flagged():
    d = SuspicionDetector()
    for _ in range(20):
        d.observe(0.01)
    v = d.check(5.0)
    assert v.suspicious and v.score > d.threshold


def test_an_ordinary_improvement_is_not_flagged():
    d = SuspicionDetector()
    for i in range(20):
        d.observe(0.01 + i * 0.0001)
    assert not d.check(0.012).suspicious


def test_one_outlier_does_not_desensitise_the_detector():
    """
    Why median and MAD rather than mean and standard deviation: an outlier
    drags the mean and inflates the deviation, so a z-score test is blunted by
    the very event it exists to catch and the second cheat sails through.
    """
    d = SuspicionDetector()
    for _ in range(20):
        d.observe(0.01)
    first = d.check(5.0)          # observed, as a real breakthrough would be
    second = d.check(5.0)
    assert first.suspicious and second.suspicious


def test_a_plateau_does_not_make_every_candidate_suspicious():
    """
    Late in a run every improvement is ~0, the scale collapses toward zero and
    any nonzero jump looks infinite. That is where a run spends most of its
    time, so a detector that flags everything there is useless.
    """
    d = SuspicionDetector()
    for _ in range(30):
        d.observe(1e-9)
    assert not d.check(2e-9).suspicious


def test_a_regression_is_not_an_improvement_to_judge():
    d = SuspicionDetector()
    for _ in range(20):
        d.observe(0.01)
    v = d.check(-0.5)
    assert not v.suspicious and "not an improvement" in v.reason


def test_no_score_change_is_reported_as_nothing_to_judge():
    assert "no score change" in SuspicionDetector().check(None).reason


def test_the_window_bounds_memory():
    d = SuspicionDetector(window=10)
    for i in range(50):
        d.observe(0.01)
    assert len(d.deltas) == 10
