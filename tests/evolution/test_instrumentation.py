"""
Installing the hooks against the real engine.

DECISIONS.md D1 accepts a real cost — hooks depend on upstream *method names*,
so a rename silently stops one firing — and claims it is mitigated by tests
that assert each hook still produces its events. That claim was not true: no
test installed the instrumentation at all, and a refactor that moved
`_install_database` and `_after_add` out of the class entirely left the whole
suite green. These tests make the claim true.

They deliberately run against the real `openevolve` classes rather than
stand-ins. A stand-in cannot catch the failure the design is exposed to, which
is upstream changing underneath us.
"""

import os

import pytest

from control_plane.telemetry import instrument as inst
from control_plane.telemetry.bus import EventBus, Sink, configure_bus, reset_bus
from control_plane.telemetry.events import EventType


class Capture(Sink):
    name = "capture"

    def __init__(self):
        self.events = []

    def write(self, events):
        self.events.extend(events)

    def types(self):
        return [e.type for e in self.events]


@pytest.fixture
def installed(monkeypatch):
    """Real hooks on real engine classes, with every event captured."""
    reset_bus()
    sink = Capture()
    bus = EventBus(sinks=[sink], flush_interval=0.01)
    monkeypatch.setattr("control_plane.telemetry.bus._bus", bus, raising=False)
    monkeypatch.setattr("control_plane.telemetry.bus._bus_pid", os.getpid(),
                        raising=False)

    engine = inst.EngineInstrumentation(run_id="run_test",
                                        experiment_id="exp_test").install()
    try:
        yield engine, sink, bus
    finally:
        engine.uninstall()
        bus.close()
        reset_bus()


# -- the hooks exist at all -------------------------------------------------

@pytest.mark.parametrize("module_path,attr,method", [
    ("openevolve.database", "ProgramDatabase", "add"),
    ("openevolve.database", "ProgramDatabase", "migrate_programs"),
    ("openevolve.database", "ProgramDatabase", "sample"),
    ("openevolve.evaluator", "Evaluator", "evaluate_program"),
    ("openevolve.llm.openai", "OpenAILLM", "generate_with_context"),
    ("openevolve.llm.openai", "OpenAILLM", "_call_api"),
    ("openevolve.controller", "OpenEvolve", "run"),
    ("openevolve.process_parallel", None, "_worker_init"),
    ("openevolve.process_parallel", None, "_run_iteration_worker"),
])
def test_every_hook_target_still_exists_upstream(module_path, attr, method):
    """
    The failure D1 accepts: an upstream rename makes a hook silently stop
    firing. This is the tripwire, and it is why the parametrisation names the
    methods rather than looping over a registry that could drift with them.
    """
    module = __import__(module_path, fromlist=["_"])
    target = getattr(module, attr) if attr else module
    assert hasattr(target, method), f"{module_path}.{attr or ''}.{method} is gone"


def test_the_instrumentation_class_keeps_its_installers():
    """
    Guards the exact break that slipped through: a module-level function
    inserted mid-class ends the class body, and every method below it silently
    stops being a method.
    """
    for name in ("install", "uninstall", "_after_add", "_install_database",
                 "_install_evaluator", "_install_llm", "_install_controller"):
        assert hasattr(inst.EngineInstrumentation, name), name


# -- the hooks actually fire ------------------------------------------------

def test_adding_a_program_emits_a_candidate_event(installed):
    engine, sink, bus = installed
    from openevolve.config import Config
    from openevolve.database import Program, ProgramDatabase

    db = ProgramDatabase(Config().database)
    db.add(Program(id="c1", code="x = 1", metrics={"combined_score": 0.5}),
           iteration=1)
    bus.flush()

    created = [e for e in sink.events if e.type == EventType.CANDIDATE_CREATED]
    assert created, "the add hook did not fire"
    assert created[0].candidate_id == "c1"
    assert created[0].run_id == "run_test"


def test_the_emitted_candidate_carries_what_the_ui_reads(installed):
    engine, sink, bus = installed
    from openevolve.config import Config
    from openevolve.database import Program, ProgramDatabase

    db = ProgramDatabase(Config().database)
    db.add(Program(id="c1", code="x = 1", parent_id="p0",
                   metrics={"combined_score": 0.75}), iteration=4)
    bus.flush()

    ev = next(e for e in sink.events if e.type == EventType.CANDIDATE_CREATED)
    assert ev.metrics.get("combined_score") == pytest.approx(0.75)
    assert ev.metadata["parent_id"] == "p0"
    assert ev.metadata["code_hash"]
    # Provenance keys are always present, null when unknown — the UI
    # distinguishes "no data" from zero and needs the key to do it.
    for key in ("generating_request_id", "generating_provider", "generating_model"):
        assert key in ev.metadata


def test_uninstall_puts_the_engine_back(installed):
    engine, sink, bus = installed
    from openevolve.database import ProgramDatabase

    assert getattr(ProgramDatabase.add, "__evolution_instrumented__", False)
    engine.uninstall()
    assert not getattr(ProgramDatabase.add, "__evolution_instrumented__", False)
    engine.install()          # leave the fixture's teardown something to undo


def test_installing_twice_does_not_double_emit(installed):
    engine, sink, bus = installed
    from openevolve.config import Config
    from openevolve.database import Program, ProgramDatabase

    inst.EngineInstrumentation(run_id="run_test").install()
    db = ProgramDatabase(Config().database)
    db.add(Program(id="c1", code="x = 1", metrics={"combined_score": 0.5}),
           iteration=1)
    bus.flush()

    created = [e for e in sink.events
               if e.type == EventType.CANDIDATE_CREATED and e.candidate_id == "c1"]
    assert len(created) == 1, "double-wrapped: every count in the UI would inflate"


def test_a_broken_hook_never_breaks_evolution(installed, monkeypatch):
    """
    Telemetry is an observer. If `_after_add` raises, the program must still be
    in the database — the engine's own work is not conditional on ours.
    """
    engine, sink, bus = installed
    from openevolve.config import Config
    from openevolve.database import Program, ProgramDatabase

    monkeypatch.setattr(
        inst.EngineInstrumentation, "_after_add",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("telemetry exploded")))

    db = ProgramDatabase(Config().database)
    db.add(Program(id="c1", code="x = 1", metrics={"combined_score": 0.5}),
           iteration=1)
    assert "c1" in db.programs
