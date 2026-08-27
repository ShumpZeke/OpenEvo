"""
Bringing shell-launched runs into the project's history.

The control plane collects events over a socket while it is running. A run
started from `./scripts/run-evolution.sh` with no Control Center open writes
its events to an NDJSON file and nothing ingests them — so that run left no
trace in the database, and "my history" silently depended on how you happened
to launch things.

This replays those files. Two properties make it safe to run on every
invocation of the memory CLI:

  * **Idempotent by construction.** `Store.ingest` is INSERT OR IGNORE on a
    unique event id, so replaying a log twice writes nothing the second time.
    The offset table is an optimisation on top of that, not the correctness
    mechanism — which matters, because an offset that goes stale (a log
    truncated and rewritten) degrades to re-reading, never to corruption.
  * **Safe on a live log.** A run still appending gets imported up to whatever
    was flushed; the next call resumes from there. A torn final line is
    skipped, as it already is in the full-rebuild path.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from ..telemetry.events import Event

# Read in chunks so a large log does not have to be held in memory at once.
_BATCH = 500


def _offset_for(store: Any, path: str) -> Tuple[int, int]:
    row = store.query_one(
        "SELECT offset, size_bytes FROM imported_logs WHERE path = ?", (path,))
    if not row:
        return 0, 0
    return int(row.get("offset") or 0), int(row.get("size_bytes") or 0)


def import_log(store: Any, path: str) -> Dict[str, Any]:
    """
    Ingest whatever is new in one NDJSON log. Returns what happened.

    Never raises for an unreadable or malformed file. History is a convenience;
    failing to read one run's log must not stop the others being read, and must
    certainly not stop the CLI printing what it does know.
    """
    result: Dict[str, Any] = {"path": path, "events": 0, "skipped": False}
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        result["skipped"] = True
        result["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return result

    offset, previous_size = _offset_for(store, path)
    if offset and size < previous_size:
        # The file shrank: it was truncated or replaced, so the old offset
        # points into different content. Start over rather than resume into
        # the middle of a line.
        offset = 0
    if offset >= size:
        result["skipped"] = True
        result["reason"] = "already imported"
        return result

    batch: List[Event] = []
    written = 0
    consumed = offset
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            for raw in fh:
                if not raw.endswith(b"\n"):
                    # A partial trailing line from a live writer. Leave the
                    # offset before it so the next call reads it whole.
                    break
                consumed += len(raw)
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    batch.append(Event.from_dict(json.loads(line)))
                except Exception:
                    continue
                if len(batch) >= _BATCH:
                    written += store.ingest(batch)
                    batch = []
        if batch:
            written += store.ingest(batch)
    except OSError as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"[:200]

    store.execute(
        "INSERT INTO imported_logs(path, offset, size_bytes, imported_at, events)"
        " VALUES (?,?,?,?,?)"
        " ON CONFLICT(path) DO UPDATE SET offset=excluded.offset,"
        " size_bytes=excluded.size_bytes, imported_at=excluded.imported_at,"
        " events=imported_logs.events + excluded.events",
        (path, consumed, size, time.time(), written),
    )
    result["events"] = written
    return result


def discover_logs(roots: Optional[List[str]] = None) -> List[str]:
    """
    Event logs written by shell-launched runs.

    `runs/` is where both launchers put their output by default. A run sent
    somewhere else with --output is not found automatically, which is honest:
    guessing at arbitrary directories would be slower and still incomplete.
    Point the importer at it explicitly instead.
    """
    found: List[str] = []
    for root in (roots or ["runs"]):
        if not os.path.isdir(root):
            continue
        for entry in sorted(os.listdir(root)):
            candidate = os.path.join(root, entry, "events.ndjson")
            if os.path.isfile(candidate):
                found.append(candidate)
    return found


def import_all(store: Any, roots: Optional[List[str]] = None) -> Dict[str, Any]:
    """Import every discoverable log. Returns a summary, never raises."""
    logs = discover_logs(roots)
    imported = 0
    files = 0
    for path in logs:
        outcome = import_log(store, path)
        if outcome.get("events"):
            imported += outcome["events"]
            files += 1
    return {"logs_found": len(logs), "files_updated": files, "events": imported}
