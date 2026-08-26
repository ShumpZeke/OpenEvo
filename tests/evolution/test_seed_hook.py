"""
Starting a run from a population instead of one program.

The forge produces variants; this is the part that puts them in the database.
Without it the forge is one more thing that is built and not in the loop.

Two behaviours carry the value and one carries the risk:

  value  the variants are spread across islands, because the problem is that
         every island starts in the same basin and migration has nothing to
         exchange for several generations
  risk   a variant that did not evaluate must be dropped, not added with zeros:
         a zeroed program occupies a MAP-Elites cell it did not earn and
         displaces one that might have
"""

import os

import pytest

from control_plane.telemetry import instrument as inst
from control_plane.telemetry import seed_hook
from control_plane.telemetry.bus import EventBus, Sink, reset_bus
from control_plane.telemetry.events import EventType

EVALUATOR = "examples/function_minimization/evaluator.py"
SEED = open("examples/function_minimization/initial_program.py").read()


class Capture(Sink):
    name = "capture"

    def __init__(self):
        self.events = []

    def write(self, events):
        self.events.extend(events)


@pytest.fixture
def seeding(monkeypatch):
    reset_bus()
    seed_hook.reset()
    monkeypatch.setenv(seed_hook.ENV_SEED_FORGE, "3")
    monkeypatch.setenv(seed_hook.ENV_EVALUATOR, EVALUATOR)

    sink = Capture()
    bus = EventBus(sinks=[sink], flush_interval=0.01)
    monkeypatch.setattr("control_plane.telemetry.bus._bus", bus, raising=False)
    monkeypatch.setattr("control_plane.telemetry.bus._bus_pid", os.getpid(),
                        raising=False)

    engine = inst.EngineInstrumentation(run_id="run_seed").install()
    try:
        yield sink, bus
    finally:
        engine.uninstall()
        bus.close()
        reset_bus()
        seed_hook.reset()


def _db(islands=4):
    from openevolve.config import Config
    from openevolve.database import ProgramDatabase

    cfg = Config().database
    cfg.num_islands = islands
    return ProgramDatabase(cfg)


def _seed_program(code=SEED):
    from openevolve.database import Program

    return Program(id="seed", code=code, metrics={"combined_score": 1.0})


# -- configuration ----------------------------------------------------------

def test_it_is_off_unless_asked_for(monkeypatch):
    monkeypatch.delenv(seed_hook.ENV_SEED_FORGE, raising=False)
    assert seed_hook.enabled() is False
    assert seed_hook.get_hook() is None


def test_the_count_is_bounded(monkeypatch):
    """
    Beyond a point the forge is spending real evaluation time before the search
    has made a single request, which is the opposite of the point.
    """
    monkeypatch.setenv(seed_hook.ENV_SEED_FORGE, "500")
    assert seed_hook.requested() == seed_hook.MAX_VARIANTS
    monkeypatch.setenv(seed_hook.ENV_SEED_FORGE, "not a number")
    assert seed_hook.requested() == 0


# -- seeding ----------------------------------------------------------------

@pytest.mark.slow
def test_the_first_add_becomes_a_population(seeding):
    sink, bus = seeding
    db = _db()
    db.add(_seed_program(), iteration=0)
    bus.flush()

    assert len(db.programs) > 1, "the run still started from one program"
    forged = [p for p in db.programs.values()
              if (p.metadata or {}).get("seed_forge")]
    assert forged
    assert all(p.metrics.get("combined_score") is not None for p in forged)


@pytest.mark.slow
def test_the_variants_are_spread_across_islands(seeding):
    """
    The whole point: upstream seeds island 0 and lets migration spread it, so
    for several generations the island structure separates identical
    populations.
    """
    sink, bus = seeding
    db = _db(islands=4)
    db.add(_seed_program(), iteration=0)

    islands = {(p.metadata or {}).get("island") for p in db.programs.values()}
    assert len(islands) > 1, f"everything landed on {islands}"


@pytest.mark.slow
def test_it_reports_what_it_added(seeding):
    sink, bus = seeding
    db = _db()
    db.add(_seed_program(), iteration=0)
    bus.flush()

    events = [e for e in sink.events if e.type == EventType.POPULATION_UPDATED
              and "seed forge" in (e.summary or "")]
    assert events
    assert events[0].metadata["seed_forge"]["accepted"] >= 1


# -- restraint --------------------------------------------------------------

def test_a_resumed_run_is_not_re_seeded(seeding):
    """
    Identified by "the database had nothing else in it", not by generation: a
    resumed run loads a populated database, and forging into it would inject
    variants of a program the search moved past generations ago.
    """
    sink, bus = seeding
    db = _db()
    db.add(_seed_program(), iteration=0)
    before = len(db.programs)

    seed_hook.reset()                    # a fresh hook, as a new process would have
    db.add(_seed_program(code=SEED + "\n# second\n"), iteration=1)
    forged_after = [p for p in db.programs.values()
                    if (p.metadata or {}).get("seed_forge")]
    assert len(db.programs) >= before
    # No *new* forge happened on the second add.
    assert len({p.metadata.get("forge_detail") for p in forged_after}) == \
        len(forged_after)


def test_it_seeds_at_most_once_per_process(seeding):
    hook = seed_hook.get_hook()
    db = _db()
    db.add(_seed_program(), iteration=0)
    assert hook.done
    assert hook.maybe_seed(db, _seed_program()) == []


def test_a_missing_evaluator_skips_rather_than_adding_unscored_programs(
        seeding, monkeypatch):
    """An unscored program cannot be compared and has no business in the archive."""
    monkeypatch.setenv(seed_hook.ENV_EVALUATOR, "/nope/evaluator.py")
    seed_hook.reset()
    db = _db()
    db.add(_seed_program(), iteration=0)
    assert len(db.programs) == 1


def test_a_forge_failure_never_costs_the_run_its_seed(seeding, monkeypatch):
    monkeypatch.setattr(
        seed_hook.SeedForgeHook, "maybe_seed",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("forge exploded")))
    db = _db()
    db.add(_seed_program(), iteration=0)
    assert "seed" in db.programs
