"""The bus must be non-blocking, bounded, fork-safe, and honest about drops."""
import json, os, time
import pytest
from control_plane.telemetry.bus import (
    EventBus, NDJSONFileSink, Sink, configure_bus, emit, get_bus, reset_bus,
)
from control_plane.telemetry.events import Component, Event, EventType


def mk(n=0):
    return Event(type=EventType.CANDIDATE_CREATED, component=Component.DATABASE,
                 summary=f"event {n}")


def test_events_reach_the_sink(workspace):
    path = os.path.join(workspace, "e.ndjson")
    bus = EventBus(sinks=[NDJSONFileSink(path)], flush_interval=0.05)
    for i in range(25):
        bus.emit(mk(i))
    bus.flush(); bus.close()
    lines = [l for l in open(path) if l.strip()]
    assert len(lines) == 25
    assert json.loads(lines[0])["type"] == "candidate.created"


def test_overflow_drops_are_counted_not_silent():
    class Slow(Sink):
        name = "slow"
        def write(self, events): time.sleep(0.05)

    bus = EventBus(sinks=[Slow()], capacity=10, batch_size=1000, flush_interval=5.0)
    for i in range(500):
        bus.emit(mk(i))
    # The contract is not "never drop" — it is "never drop silently".
    assert bus.stats.dropped_overflow > 0
    assert bus.stats.emitted + bus.stats.dropped_overflow == 500
    bus.close()


def test_a_failing_sink_cannot_break_emission():
    class Broken(Sink):
        name = "broken"
        def write(self, events): raise RuntimeError("sink down")

    bus = EventBus(sinks=[Broken()], flush_interval=0.05)
    for i in range(10):
        assert bus.emit(mk(i)) is True   # emission still succeeds
    bus.flush(); time.sleep(0.2)
    assert bus.stats.sink_errors > 0
    bus.close()


def test_redaction_happens_before_the_sink(workspace):
    path = os.path.join(workspace, "e.ndjson")
    bus = EventBus(sinks=[NDJSONFileSink(path)], flush_interval=0.05)
    bus.emit(Event(type=EventType.MODEL_REQUEST_COMPLETED, component=Component.LLM,
                   metadata={"api_key": "sk-abcdefghijklmnop1234567890"}))
    bus.flush(); bus.close()
    assert "sk-abcdefghijklmnop" not in open(path).read()


def test_sampling_is_deterministic():
    bus = EventBus(sinks=[], flush_interval=5.0)
    bus.set_sample_rate(EventType.RESOURCE_CPU, 0.5)
    kept = sum(
        bus.emit(Event(type=EventType.RESOURCE_CPU, component=Component.RESOURCE))
        for _ in range(100)
    )
    assert 40 <= kept <= 60
    bus.close()


def test_configure_bus_is_idempotent_per_process(workspace):
    a = configure_bus(ndjson_path=os.path.join(workspace, "a.ndjson"))
    b = configure_bus(ndjson_path=os.path.join(workspace, "b.ndjson"))
    assert a is b
    reset_bus()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork only")
def test_bus_is_rebuilt_after_fork(workspace):
    """
    A forked child inherits the bus object but not its worker thread. If the
    child reused it, worker telemetry would queue forever and be lost — the
    exact bug that made model requests invisible during integration testing.
    """
    path = os.path.join(workspace, "forked.ndjson")
    configure_bus(ndjson_path=path)
    emit(mk(0))

    pid = os.fork()
    if pid == 0:
        try:
            # Inherited bus must be treated as absent...
            assert get_bus() is None
            # ...and reconfiguring must produce a working one.
            child = configure_bus(ndjson_path=path)
            child.emit(mk(1))
            child.flush(); child.close()
            os._exit(0)
        except BaseException:
            os._exit(1)

    _, status = os.waitpid(pid, 0)
    assert os.WEXITSTATUS(status) == 0, "child failed to rebuild the bus"
    bus = get_bus()
    bus.flush(); bus.close()
    assert len([l for l in open(path) if l.strip()]) == 2
    reset_bus()
