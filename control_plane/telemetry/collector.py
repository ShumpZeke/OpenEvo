"""
Event collector.

Runs inside the API process and is the single writer to SQLite. It accepts
events from two directions:

  socket  engine and worker processes connect to a loopback TCP port and stream
          NDJSON. This is the live path.
  log     every run's events.ndjson is also tailed. This is the durable path and
          the recovery path: if the API was down while a run was executing, or
          the socket dropped mid-run, tailing backfills everything that was
          missed. Ingest is idempotent on event_id, so overlap between the two
          paths is harmless.

Belt and braces is deliberate. A control plane that loses events when it
restarts would show an incomplete history, and section 36 treats a chart that
misrepresents reality as a defect rather than a cosmetic issue.
"""

from __future__ import annotations

import json
import os
import socketserver
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set

from ..storage.store import Store
from .events import Event

Subscriber = Callable[[List[Event]], None]


class EventCollector:
    def __init__(self, store: Store, batch_size: int = 200,
                 flush_interval: float = 0.2) -> None:
        self.store = store
        self._buffer: List[Event] = []
        self._lock = threading.Lock()
        self._subscribers: Set[Subscriber] = set()
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._stop = threading.Event()
        self._server: Optional[socketserver.ThreadingTCPServer] = None
        self._tails: Dict[str, threading.Thread] = {}
        self.stats = {
            "received": 0, "ingested": 0, "duplicates": 0,
            "parse_errors": 0, "ingest_errors": 0, "last_error": None,
        }
        self._flusher = threading.Thread(target=self._flush_loop,
                                         name="evolution-collector", daemon=True)
        self._flusher.start()

    # -- subscribers ---------------------------------------------------

    def subscribe(self, fn: Subscriber) -> Subscriber:
        with self._lock:
            self._subscribers.add(fn)
        return fn

    def unsubscribe(self, fn: Subscriber) -> None:
        with self._lock:
            self._subscribers.discard(fn)

    # -- intake --------------------------------------------------------

    def submit(self, events: List[Event]) -> None:
        if not events:
            return
        with self._lock:
            self._buffer.extend(events)
            self.stats["received"] += len(events)

    def submit_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            self.submit([Event.from_dict(json.loads(line))])
        except Exception:
            self.stats["parse_errors"] += 1

    def _flush_loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(self._flush_interval)
            self._flush()
        self._flush()

    def _flush(self) -> None:
        with self._lock:
            if not self._buffer:
                return
            batch, self._buffer = self._buffer, []
            subs = list(self._subscribers)
        try:
            written = self.store.ingest(batch)
            self.stats["ingested"] += written
            self.stats["duplicates"] += len(batch) - written
        except Exception as exc:
            self.stats["ingest_errors"] += 1
            self.stats["last_error"] = repr(exc)
        # Fan out even if persistence failed: a live viewer seeing the event is
        # still better than silence, and the durable log is unaffected.
        for fn in subs:
            try:
                fn(batch)
            except Exception:
                pass

    # -- socket server -------------------------------------------------

    def serve_socket(self, host: str = "127.0.0.1", port: int = 8770) -> int:
        collector = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                buf = b""
                while not collector._stop.is_set():
                    try:
                        chunk = self.request.recv(65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    buf += chunk
                    *lines, buf = buf.split(b"\n")
                    for raw in lines:
                        if raw:
                            collector.submit_line(raw.decode("utf-8", "replace"))

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._server = Server((host, port), Handler)
        actual_port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, name="evolution-ingest",
                         daemon=True).start()
        return actual_port

    # -- log tailing ---------------------------------------------------

    def tail_log(self, path: str, from_start: bool = True) -> None:
        """
        Follow an NDJSON event log.

        Reading from the start on attach is what backfills a run the API missed.
        Duplicate events are dropped by the store's INSERT OR IGNORE, so this is
        safe to call on a log the socket path is already delivering.
        """
        if path in self._tails:
            return

        def run() -> None:
            pos = 0 if from_start else None
            while not self._stop.is_set():
                try:
                    if not os.path.exists(path):
                        time.sleep(0.5)
                        continue
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        if pos is None:
                            fh.seek(0, os.SEEK_END)
                            pos = fh.tell()
                        else:
                            fh.seek(pos)
                        chunk = fh.read()
                        pos = fh.tell()
                    if chunk:
                        lines = chunk.split("\n")
                        # A trailing partial line is re-read next pass.
                        if not chunk.endswith("\n"):
                            pos -= len(lines[-1].encode("utf-8"))
                            lines = lines[:-1]
                        for line in lines:
                            self.submit_line(line)
                    else:
                        time.sleep(0.25)
                except Exception:
                    time.sleep(1.0)

        t = threading.Thread(target=run, name=f"tail-{os.path.basename(path)}", daemon=True)
        self._tails[path] = t
        t.start()

    def stop_tail(self, path: str) -> None:
        self._tails.pop(path, None)

    def close(self) -> None:
        self._stop.set()
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
        self._flush()

    def health(self) -> Dict[str, Any]:
        with self._lock:
            pending = len(self._buffer)
            subs = len(self._subscribers)
        return {
            **self.stats,
            "pending": pending,
            "subscribers": subs,
            "tailed_logs": list(self._tails.keys()),
        }
