"""
Verification during a live run: which candidates get checked, and what happens
when one fails.

The gating is the whole design. Verifying everything trades away the
throughput the rest of the system exists to buy; verifying nothing means the
first candidate that games the metric becomes the champion and every
generation after it is built on that.

The other load-bearing choice is what a failure does — which is *report*, not
delete. Instrumentation that silently removes the engine's work would make the
fork behave differently from upstream in a way no test would catch.
"""

import os

import pytest

from control_plane.telemetry import instrument as inst
from control_plane.telemetry import verification_hook as vh
from control_plane.telemetry.bus import EventBus, Sink, reset_bus
from control_plane.telemetry.events import EventType

OBJECTIVE = '''
import numpy as np
def evaluate_function(x, y):
    return np.sin(x)*np.cos(y) + np.sin(x*y) + (x**2 + y**2)/20
'''

HONEST = OBJECTIVE + '''
def search_algorithm(iterations=50, bounds=(-5, 5)):
    import numpy as np
    best = None
    for _ in range(iterations):
        x = np.random.uniform(bounds[0], bounds[1])
        y = np.random.uniform(bounds[0], bounds[1])
        v = evaluate_function(x, y)
        if best is None or v < best[2]:
            best = (x, y, v)
    return best
def run_search():
    return search_algorithm()
'''

CHEAT = OBJECTIVE + '''
def search_algorithm(iterations=50, bounds=(-5, 5)):
    return 0.0, 0.0, -999.0
def run_search():
    return search_algorithm()
'''


class Capture(Sink):
    name = "capture"

    def __init__(self):
        self.events = []

    def write(self, events):
        self.events.extend(events)

    def types(self):
        return [e.type for e in self.events]


@pytest.fixture
def verifying(monkeypatch):
    """Instrumentation installed with verification enabled for this task."""
    reset_bus()
    vh.reset()
    monkeypatch.setenv(vh.ENV_VERIFY, "1")
    monkeypatch.setenv(vh.ENV_EVALUATOR, "examples/function_minimization/evaluator.py")
    monkeypatch.setenv(vh.ENV_ENTRY_POINT, "search_algorithm")

    sink = Capture()
    bus = EventBus(sinks=[sink], flush_interval=0.01)
    monkeypatch.setattr("control_plane.telemetry.bus._bus", bus, raising=False)
    monkeypatch.setattr("control_plane.telemetry.bus._bus_pid", os.getpid(),
                        raising=False)

    engine = inst.EngineInstrumentation(run_id="run_verify").install()
    try:
        yield sink, bus
    finally:
        engine.uninstall()
        bus.close()
        reset_bus()
        vh.reset()


def _db():
    from openevolve.config import Config
    from openevolve.database import ProgramDatabase

    return ProgramDatabase(Config().database)


def _variant(code, tag):
    """
    Same behaviour, different text.

    Identical code lands in the same MAP-Elites cell, so the second add
    displaces the first, upstream removes the orphan, and `best_program_id`
    points at a program that no longer exists — at which point the newcomer
    becomes champion no matter how it scored. Not a bug, but it makes any test
    about "is this a new champion" measure the wrong thing.
    """
    return f"# variant {tag}\n" + code


def _program(pid, code, score, parent_score=None):
    from openevolve.database import Program

    metadata = {}
    if parent_score is not None:
        metadata["parent_metrics"] = {"combined_score": parent_score}
    return Program(id=pid, code=code, metrics={"combined_score": score},
                   metadata=metadata)


# -- gating -----------------------------------------------------------------

def test_it_is_off_unless_asked_for(monkeypatch):
    monkeypatch.delenv(vh.ENV_VERIFY, raising=False)
    vh.reset()
    assert vh.enabled() is False
    assert vh.get_verifier() is None


def test_a_new_champion_is_always_verified(verifying):
    sink, bus = verifying
    db = _db()
    db.add(_program("c1", HONEST, 0.5), iteration=1)
    bus.flush()

    started = [e for e in sink.events
               if e.type == EventType.CANDIDATE_VERIFICATION_STARTED]
    assert started, "the first candidate is the champion and must be verified"
    assert started[0].metadata["trigger"] == "new_champion"


def test_an_ordinary_candidate_is_not_verified(verifying):
    """Otherwise the cost is paid on every candidate in the run."""
    sink, bus = verifying
    db = _db()
    db.add(_program("champ", HONEST, 0.9), iteration=1)
    # Flush before clearing: the bus is asynchronous, so the champion's own
    # events would otherwise arrive *after* the clear and be read as the second
    # candidate's.
    bus.flush()
    sink.events.clear()

    # Lower score, so not a champion; small delta, so not unusual.
    db.add(_program("c2", _variant(HONEST, "b"), 0.5, parent_score=0.49), iteration=2)
    bus.flush()

    assert EventType.CANDIDATE_VERIFICATION_STARTED not in sink.types()


def test_a_suspicious_jump_is_flagged_and_verified(verifying):
    sink, bus = verifying
    db = _db()
    live = vh.get_verifier()
    for _ in range(20):
        live.detector.observe(0.001)

    db.add(_program("champ", HONEST, 0.99), iteration=1)
    bus.flush()
    sink.events.clear()
    # Not a champion (0.5 < 0.99) but a huge jump from its own parent.
    db.add(_program("c2", _variant(HONEST, "b"), 0.5, parent_score=0.01), iteration=2)
    bus.flush()

    assert EventType.CANDIDATE_SUSPICIOUS in sink.types()
    started = [e for e in sink.events
               if e.type == EventType.CANDIDATE_VERIFICATION_STARTED]
    assert started and started[0].metadata["trigger"] == "suspicious_jump"


# -- outcomes ---------------------------------------------------------------

def test_an_honest_champion_passes(verifying):
    sink, bus = verifying
    _db().add(_program("c1", HONEST, 0.5), iteration=1)
    bus.flush()

    assert EventType.CANDIDATE_VERIFICATION_PASSED in sink.types()
    assert EventType.CANDIDATE_VERIFICATION_FAILED not in sink.types()


def test_a_cheating_champion_fails_with_the_counterexample(verifying):
    sink, bus = verifying
    _db().add(_program("c1", CHEAT, 0.99), iteration=1)
    bus.flush()

    failed = [e for e in sink.events
              if e.type == EventType.CANDIDATE_VERIFICATION_FAILED]
    assert failed, "a candidate reporting a value it never computed must fail"
    payload = failed[0].metadata
    assert payload["failures"], "the failing check must be named"
    assert any("reported" in f["message"] for f in payload["failures"])


def test_a_failed_candidate_stays_in_the_population(verifying):
    """
    Reporting, not enforcement. Instrumentation that deleted the engine's work
    would make this fork behave differently from upstream, invisibly.
    """
    sink, bus = verifying
    db = _db()
    db.add(_program("c1", CHEAT, 0.99), iteration=1)
    bus.flush()

    assert EventType.CANDIDATE_VERIFICATION_FAILED in sink.types()
    assert "c1" in db.programs


def test_a_champion_is_not_re_verified_on_every_confirmation(verifying):
    sink, bus = verifying
    db = _db()
    db.add(_program("c1", HONEST, 0.5), iteration=1)
    db.add(_program("c1", HONEST, 0.5), iteration=2)
    bus.flush()

    passed = [e for e in sink.events
              if e.type in (EventType.CANDIDATE_VERIFICATION_PASSED,
                            EventType.CANDIDATE_VERIFICATION_FAILED)]
    assert len(passed) == 1


def test_verification_failing_cannot_break_the_run(verifying, monkeypatch):
    sink, bus = verifying
    live = vh.get_verifier()
    monkeypatch.setattr(live, "verify",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))

    db = _db()
    db.add(_program("c1", HONEST, 0.5), iteration=1)
    assert "c1" in db.programs


def test_the_verifier_is_rebuilt_after_a_fork(verifying, monkeypatch):
    """
    A forked worker inherits this module's globals, including a verifier
    carrying the parent's suspicion history. Verification belongs to the
    process where `add` happens.
    """
    first = vh.get_verifier()
    assert first is not None
    monkeypatch.setattr(vh, "_active_pid", os.getpid() + 1)
    assert vh.get_verifier() is not first
