"""
Event transport.

Hard requirement (SOURCE_OF_TRUTH section 25): telemetry must not cripple
evolution throughput. Every design choice here follows from that:

  * Emission is non-blocking. The engine thread appends to a bounded queue and
    returns; it never waits on disk, socket or database.
  * Overflow drops, and drops are *counted and reported*. A silently lossy
    pipeline would make the UI lie, which section 36 forbids; a visible drop
    counter keeps the UI honest.
  * A background worker batches to sinks, so per-event syscall cost amortises.
  * Sink failure is contained. A broken sink is disabled and reported; it never
    propagates into the engine.

Transport across processes: OpenEvolve evaluates candidates in worker processes
(process_parallel.py), so emitters exist in several processes at once. Each
writes NDJSON to a shared append-only log (durable, replayable, portable to
Windows) and, when a collector is listening, mirrors to a TCP loopback socket
for low-latency live streaming. TCP rather than AF_UNIX because Windows is a
first-class target.
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .events import Component, Event, EventType, Status
from .redaction import Redactor, default_redactor


@dataclass
class BusStats:
    """Telemetry self-health — surfaced on the System page."""

    emitted: int = 0
    delivered: int = 0
    dropped_overflow: int = 0
    dropped_sampled: int = 0
    sink_errors: int = 0
    queue_depth: int = 0
    queue_capacity: int = 0
    max_queue_depth: int = 0
    last_error: Optional[str] = None
    started_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        d["uptime_s"] = time.time() - self.started_at
        rate = d["uptime_s"] and (self.emitted / d["uptime_s"]) or 0.0
        d["emit_rate_per_s"] = round(rate, 3)
        total = self.emitted or 1
        d["drop_ratio"] = round(
            (self.dropped_overflow + self.dropped_sampled) / total, 6
        )
        return d


class Sink:
    """A destination for batches of events."""

    name = "sink"

    def write(self, events: List[Event]) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:
        pass


class NDJSONFileSink(Sink):
    """
    Append-only durable log. This is the replay source of truth: the SQLite
    store is an index built over it, and can be rebuilt from it.
    """

    name = "ndjson"

    def __init__(self, path: str, fsync_every: int = 0) -> None:
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        # Line-buffered append; multiple processes append to the same file.
        # Writes of a single line under PIPE_BUF are atomic enough on POSIX,
        # and each write() call below emits one batch as one buffer.
        self._fh = open(path, "a", encoding="utf-8")
        self._fsync_every = fsync_every
        self._since_sync = 0
        self._lock = threading.Lock()

    def write(self, events: List[Event]) -> None:
        payload = "".join(
            json.dumps(e.to_dict(), separators=(",", ":"), default=str) + "\n"
            for e in events
        )
        with self._lock:
            self._fh.write(payload)
            self._fh.flush()
            self._since_sync += len(events)
            if self._fsync_every and self._since_sync >= self._fsync_every:
                os.fsync(self._fh.fileno())
                self._since_sync = 0

    def close(self) -> None:
        with self._lock:
            try:
                self._fh.flush()
                self._fh.close()
            except Exception:
                pass


class SocketSink(Sink):
    """
    Best-effort low-latency mirror to a listening collector over TCP loopback.

    "Best effort" is deliberate: if the control plane is not running, evolution
    must proceed untouched. A dead socket disables this sink; the NDJSON log
    still has every event, so nothing is lost — only live latency degrades.
    """

    name = "socket"

    def __init__(self, host: str = "127.0.0.1", port: int = 8770, timeout: float = 1.0):
        self.host, self.port, self.timeout = host, port, timeout
        self._sock: Optional[socket.socket] = None
        self._failed_at: float = 0.0
        self._retry_after = 5.0

    def _connect(self) -> Optional[socket.socket]:
        if self._sock is not None:
            return self._sock
        if time.time() - self._failed_at < self._retry_after:
            return None
        try:
            s = socket.create_connection((self.host, self.port), timeout=self.timeout)
            s.settimeout(self.timeout)
            self._sock = s
            return s
        except OSError:
            self._failed_at = time.time()
            return None

    def write(self, events: List[Event]) -> None:
        s = self._connect()
        if s is None:
            return
        payload = "".join(
            json.dumps(e.to_dict(), separators=(",", ":"), default=str) + "\n"
            for e in events
        ).encode("utf-8")
        try:
            s.sendall(payload)
        except OSError:
            try:
                s.close()
            except OSError:
                pass
            self._sock = None
            self._failed_at = time.time()

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


class CallbackSink(Sink):
    """In-process fanout, used by the API server to feed SSE subscribers."""

    name = "callback"

    def __init__(self, fn: Callable[[List[Event]], None]) -> None:
        self._fn = fn

    def write(self, events: List[Event]) -> None:
        self._fn(events)


class EventBus:
    """
    Bounded, batching, non-blocking event bus.

    Ordering: events are delivered to sinks in emission order per process. The
    NDJSON log interleaves processes, which is expected — consumers order by
    (timestamp, event_id) and correlate by trace_id.
    """

    def __init__(
        self,
        sinks: Optional[List[Sink]] = None,
        capacity: int = 20000,
        batch_size: int = 256,
        flush_interval: float = 0.25,
        redactor: Optional[Redactor] = None,
        sample_rates: Optional[Dict[EventType, float]] = None,
    ) -> None:
        self._q: "queue.Queue[Optional[Event]]" = queue.Queue(maxsize=capacity)
        self._sinks: List[Sink] = list(sinks or [])
        self._disabled_sinks: Dict[str, str] = {}
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._redactor = redactor or default_redactor()
        self._sample_rates = dict(sample_rates or {})
        self.stats = BusStats(queue_capacity=capacity)
        self._stop = threading.Event()
        self._counter = 0
        self._worker = threading.Thread(
            target=self._run, name="evolution-telemetry", daemon=True
        )
        self._worker.start()
        atexit.register(self.close)

    # -- emission ------------------------------------------------------

    def emit(self, event: Event) -> bool:
        """
        Queue an event. Returns False if it was dropped.

        Never raises: a telemetry failure must not surface inside the engine.
        """
        try:
            if self._should_sample_out(event):
                self.stats.dropped_sampled += 1
                return False
            # Redact before the event can reach any sink.
            self._redactor.redact_event(event)
            self._q.put_nowait(event)
            self.stats.emitted += 1
            depth = self._q.qsize()
            self.stats.queue_depth = depth
            if depth > self.stats.max_queue_depth:
                self.stats.max_queue_depth = depth
            return True
        except queue.Full:
            self.stats.dropped_overflow += 1
            return False
        except Exception as exc:  # defensive: never break the caller
            self.stats.sink_errors += 1
            self.stats.last_error = f"emit: {exc!r}"
            return False

    def _should_sample_out(self, event: Event) -> bool:
        rate = self._sample_rates.get(event.type)
        if rate is None or rate >= 1.0:
            return False
        if rate <= 0.0:
            return True
        # Deterministic decimation rather than RNG: predictable, reproducible,
        # and cheap. Reproducibility matters for replaying a run.
        self._counter += 1
        return (self._counter % max(1, int(round(1.0 / rate)))) != 0

    def set_sample_rate(self, event_type: EventType, rate: float) -> None:
        self._sample_rates[event_type] = rate

    # -- sinks ---------------------------------------------------------

    def add_sink(self, sink: Sink) -> None:
        self._sinks.append(sink)

    def remove_sink(self, sink: Sink) -> None:
        if sink in self._sinks:
            self._sinks.remove(sink)

    # -- worker --------------------------------------------------------

    def _run(self) -> None:
        batch: List[Event] = []
        last_flush = time.time()
        while not self._stop.is_set():
            timeout = max(0.01, self._flush_interval - (time.time() - last_flush))
            try:
                item = self._q.get(timeout=timeout)
                if item is None:  # shutdown sentinel
                    break
                batch.append(item)
            except queue.Empty:
                pass

            due = (
                len(batch) >= self._batch_size
                or (batch and time.time() - last_flush >= self._flush_interval)
            )
            if due:
                self._flush(batch)
                batch = []
                last_flush = time.time()

        # Drain whatever is left so a clean shutdown loses nothing.
        while True:
            try:
                item = self._q.get_nowait()
                if item is not None:
                    batch.append(item)
            except queue.Empty:
                break
        if batch:
            self._flush(batch)

    def _flush(self, batch: List[Event]) -> None:
        self.stats.queue_depth = self._q.qsize()
        for sink in list(self._sinks):
            if sink.name in self._disabled_sinks:
                continue
            try:
                sink.write(batch)
            except Exception as exc:
                self.stats.sink_errors += 1
                self.stats.last_error = f"{sink.name}: {exc!r}"
                # Contain a persistently broken sink rather than spinning on it.
                if self.stats.sink_errors > 50:
                    self._disabled_sinks[sink.name] = repr(exc)
        self.stats.delivered += len(batch)

    def flush(self, timeout: float = 5.0) -> bool:
        """Block until the queue drains. Used by tests and clean shutdown."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._q.empty():
                time.sleep(self._flush_interval + 0.05)
                return True
            time.sleep(0.01)
        return False

    def close(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass
        self._worker.join(timeout=5.0)
        for sink in self._sinks:
            try:
                sink.close()
            except Exception:
                pass

    def health_event(self) -> Event:
        return Event(
            type=EventType.TELEMETRY_HEALTH,
            component=Component.TELEMETRY,
            status=Status.OK if not self._disabled_sinks else Status.WARNING,
            summary="telemetry self-health",
            metrics={
                k: float(v)
                for k, v in self.stats.to_dict().items()
                if isinstance(v, (int, float))
            },
            metadata={
                "disabled_sinks": dict(self._disabled_sinks),
                "last_error": self.stats.last_error,
            },
        )


# --------------------------------------------------------------------------
# Process-wide bus
# --------------------------------------------------------------------------

_bus: Optional[EventBus] = None
_bus_pid: Optional[int] = None
_bus_lock = threading.Lock()


def get_bus() -> Optional[EventBus]:
    # A bus inherited across fork() is unusable — see _reset_after_fork.
    if _bus is not None and _bus_pid != os.getpid():
        return None
    return _bus


def configure_bus(
    ndjson_path: Optional[str] = None,
    socket_port: Optional[int] = None,
    capacity: int = 20000,
    extra_sinks: Optional[List[Sink]] = None,
) -> EventBus:
    """
    Install the process-wide bus. Idempotent per process.

    "Per process" is load-bearing. OpenEvolve evaluates candidates in a
    ProcessPoolExecutor, and on POSIX those workers are forked. A forked child
    inherits this module's globals — including a fully-formed EventBus object —
    but NOT the bus's worker thread, which does not survive fork. The child
    would then see `_bus is not None`, skip setup, and queue every event into a
    buffer nothing drains: worker telemetry silently disappears.

    So the bus is keyed by owning PID, and a stale inherited bus is rebuilt
    rather than reused. `os.register_at_fork` clears it eagerly where available;
    the PID check below is the portable backstop and also covers `spawn`.
    """
    global _bus, _bus_pid
    with _bus_lock:
        if _bus is not None and _bus_pid == os.getpid():
            return _bus
        if _bus is not None:
            # Inherited from a parent process: drop it without touching the
            # parent's file handles or flushing its queue.
            _bus = None
        sinks: List[Sink] = []
        if ndjson_path:
            sinks.append(NDJSONFileSink(ndjson_path))
        if socket_port:
            sinks.append(SocketSink(port=socket_port))
        sinks.extend(extra_sinks or [])
        _bus = EventBus(sinks=sinks, capacity=capacity)
        _bus_pid = os.getpid()
        return _bus


def reset_bus() -> None:
    """Tear down the process-wide bus (tests, and re-configuration)."""
    global _bus, _bus_pid
    with _bus_lock:
        if _bus is not None and _bus_pid == os.getpid():
            _bus.close()
        _bus = None
        _bus_pid = None


def _reset_after_fork() -> None:
    """
    Drop the inherited bus in a forked child.

    Deliberately does NOT close it: the parent still owns those file handles and
    sockets, and closing them here would corrupt the parent's telemetry.
    """
    global _bus, _bus_pid
    _bus = None
    _bus_pid = None


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)


def emit(event: Event) -> bool:
    """
    Emit through the process-wide bus.

    A no-op when no bus is configured, which is what makes the instrumentation
    free for users running the plain upstream CLI.
    """
    bus = get_bus()   # returns None for a bus inherited across fork()
    if bus is None:
        return False
    return bus.emit(event)
