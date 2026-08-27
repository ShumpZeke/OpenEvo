"""
SQLite-backed control-plane store.

Single-writer by design: the API process owns the connection and ingests every
event. Engine and worker processes never touch SQLite — they emit to the NDJSON
log and the loopback collector. This sidesteps SQLite's multi-writer weakness
entirely instead of fighting it with retries.

Reads are concurrent (WAL) and go through short-lived read connections.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..telemetry.events import Event, EventType, Status
from .schema import ADDITIVE_COLUMNS, SCHEMA, SCHEMA_VERSION, STRUCTURAL_CHANGES

logger = logging.getLogger(__name__)


def _j(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str)


def _loads(text: Optional[str], default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return default


def _excerpt(text: Any, limit: int = 4000) -> Optional[str]:
    if text is None:
        return None
    s = text if isinstance(text, str) else _j(text)
    return s if len(s) <= limit else s[:limit] + f"\n… «truncated {len(s) - limit} chars»"


def _process_alive(pid: int) -> bool:
    """
    Whether a recorded engine PID is still this run's process.

    Signal 0 alone is not enough: PIDs are reused, and a recycled one would
    keep a dead run marked "running" indefinitely — the exact failure being
    fixed. Where /proc is readable the command line is checked too, so a PID
    now belonging to something unrelated is correctly treated as gone.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # alive, owned by someone else
    except OSError as exc:
        # Windows raises OSError(EINVAL) -- WinError 87 -- for a PID that does
        # not exist, where POSIX raises ProcessLookupError. That specific errno
        # is the same evidence, so it is read the same way.
        if exc.errno == errno.EINVAL:
            return False
        # Any other OSError means the question was not answered. Claiming the
        # process is gone would mark a running engine dead and reconcile a live
        # run out from under itself, so the unknown case still says "alive".
        return True

    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            cmdline = fh.read().decode("utf-8", "replace")
    except OSError:
        return True          # no /proc (macOS, Windows); the signal is all we have
    return "control_plane.runner.entrypoint" in cmdline or "openevolve" in cmdline


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._write_lock = threading.Lock()
        self._conn = self._connect()
        self._migrate()
        self._local = threading.local()

    # -- connections ---------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def reader(self) -> sqlite3.Connection:
        """Thread-local read connection (WAL allows concurrent readers)."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def _migrate(self) -> None:
        with self._write_lock:
            previous = self._stored_version()
            # Columns first, then the script. SCHEMA creates indexes over the
            # newer columns (ix_cand_gen_model), and CREATE INDEX on a table
            # that predates them fails outright — so reconciling afterwards
            # never runs. On a new database there is no table yet and this is
            # a no-op.
            self._reconcile_columns()
            self._conn.executescript(SCHEMA)
            if previous is not None and previous < SCHEMA_VERSION:
                for version, what in sorted(STRUCTURAL_CHANGES.items()):
                    if previous < version:
                        # Not applied automatically: rebuilding needs the event
                        # log, which the Store does not own. Saying so is better
                        # than a projection that quietly serves old shapes.
                        logger.warning(
                            "Storage opened at schema v%s (now v%s): %s. "
                            "Run Store.rebuild_projections_from_log() against "
                            "this workspace's NDJSON log to correct it.",
                            previous, SCHEMA_VERSION, what,
                        )
            self._conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES('version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()

    def _stored_version(self) -> Optional[int]:
        try:
            row = self._conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
        except sqlite3.OperationalError:
            return None          # brand new database; no schema_meta yet
        try:
            return int(row[0]) if row else None
        except (TypeError, ValueError):
            return None

    def _reconcile_columns(self) -> None:
        """
        Add columns that a table gained after the workspace was created.

        Without this, opening an older workspace leaves `candidates` without
        `gen_request_id` and every attribution query fails on it — not at
        startup, but whenever someone opens the view that reads it.
        """
        for table, columns in ADDITIVE_COLUMNS.items():
            try:
                present = {r[1] for r in self._conn.execute(
                    f"PRAGMA table_info({table})")}
            except sqlite3.OperationalError:
                continue          # table not created yet; SCHEMA will make it
            if not present:
                continue
            for name, decl in columns.items():
                if name in present:
                    continue
                logger.info("Storage: adding %s.%s (%s)", table, name, decl)
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def close(self) -> None:
        with self._write_lock:
            self._conn.close()

    # -- ingest --------------------------------------------------------

    def ingest(self, events: Sequence[Event]) -> int:
        """
        Persist a batch of events and apply their projections atomically.

        Returns the number of events newly written (duplicates are skipped, so
        replaying the NDJSON log is idempotent).
        """
        if not events:
            return 0
        written = 0
        with self._write_lock:
            cur = self._conn.cursor()
            try:
                for ev in events:
                    d = ev.to_dict()
                    cur.execute(
                        """INSERT OR IGNORE INTO events
                           (event_id, trace_id, span_id, parent_span_id, experiment_id,
                            run_id, generation, iteration, candidate_id, island_id,
                            timestamp, duration_ms, component, type, status, summary,
                            payload, pid)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            d["event_id"], d.get("trace_id"), d.get("span_id"),
                            d.get("parent_span_id"), d.get("experiment_id"),
                            d.get("run_id"), d.get("generation"), d.get("iteration"),
                            d.get("candidate_id"), d.get("island_id"),
                            d["timestamp"], d.get("duration_ms"), d["component"],
                            d["type"], d["status"], d.get("summary"),
                            _j(d), d.get("pid"),
                        ),
                    )
                    if cur.rowcount:
                        written += 1
                        self._project(cur, ev)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return written

    # -- projections ---------------------------------------------------

    def _project(self, cur: sqlite3.Cursor, ev: Event) -> None:
        """Fold one event into the current-state tables."""
        t = ev.type
        run_id = ev.run_id

        if t in (EventType.EXPERIMENT_CREATED,):
            self._project_experiment(cur, ev)

        elif t in (
            EventType.EXPERIMENT_STARTED,
            EventType.EXPERIMENT_STOPPED,
            EventType.EXPERIMENT_COMPLETED,
            EventType.EXPERIMENT_FAILED,
            EventType.EXPERIMENT_PAUSED,
            EventType.EXPERIMENT_RESUMED,
        ):
            self._project_run_status(cur, ev)

        elif t == EventType.CANDIDATE_CREATED:
            self._project_candidate(cur, ev)

        elif t == EventType.CANDIDATE_EVALUATION_COMPLETED:
            self._project_candidate_metrics(cur, ev)

        elif t == EventType.CANDIDATE_BEST_UPDATED:
            self._project_best(cur, ev)

        elif t in (EventType.EVALUATOR_COMPLETED, EventType.EVALUATOR_FAILED,
                   EventType.EVALUATOR_STARTED):
            self._project_evaluation(cur, ev)

        elif t in (EventType.MODEL_REQUEST_COMPLETED, EventType.MODEL_REQUEST_FAILED,
                   EventType.MODEL_RATE_LIMITED):
            self._project_model_request(cur, ev)

        elif t in (EventType.ISLAND_UPDATED, EventType.ISLAND_CREATED):
            self._project_island(cur, ev)

        elif t == EventType.ISLAND_MIGRATION_COMPLETED:
            self._project_migration(cur, ev)

        elif t in (EventType.MAP_ELITES_CELL_UPDATED,
                   EventType.MAP_ELITES_ELITE_REPLACED):
            self._project_map_elites(cur, ev)

        elif t == EventType.CHECKPOINT_CREATED:
            self._project_checkpoint(cur, ev)

        elif t in (EventType.SANDBOX_CREATED, EventType.SANDBOX_COMPLETED,
                   EventType.SANDBOX_DESTROYED, EventType.SANDBOX_STARTED):
            self._project_sandbox(cur, ev)

        elif t in (EventType.OPENCODE_AGENT_STARTED, EventType.OPENCODE_AGENT_COMPLETED,
                   EventType.OMO_MEMBER_STARTED, EventType.OMO_MEMBER_COMPLETED):
            self._project_agent_run(cur, ev)

        elif t.value.startswith("resource."):
            cur.execute(
                "INSERT INTO resource_metrics(run_id, timestamp, kind, value, unit, scope, metadata)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    run_id, ev.timestamp, t.value.split(".", 1)[1],
                    float(ev.metrics.get("value", 0.0)),
                    ev.metadata.get("unit"), ev.metadata.get("scope"), _j(ev.metadata),
                ),
            )

        # Runs advance on generation boundaries even when nothing else changed.
        if t == EventType.GENERATION_COMPLETED and run_id:
            # Upstream numbers iterations from 0 (`start_iteration = 0` in
            # controller.py), so finishing iteration 11 of a 12-iteration run
            # means 12 are done. Storing the index would leave every completed
            # run reading one short of its target — a wrong number rather than
            # a missing one, which is the failure mode this project treats as
            # worse than showing nothing.
            index = ev.iteration if ev.iteration is not None else ev.generation
            if index is not None:
                cur.execute(
                    "UPDATE runs SET iterations_done ="
                    " MAX(COALESCE(iterations_done,0), ?) WHERE run_id = ?",
                    (int(index) + 1, run_id),
                )

    def _project_experiment(self, cur: sqlite3.Cursor, ev: Event) -> None:
        md = ev.metadata
        cur.execute(
            """INSERT INTO experiments
               (experiment_id, name, created_at, config_path, config_revision,
                initial_program, evaluator_path, status, metadata)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(experiment_id) DO UPDATE SET
                 name=excluded.name, status=excluded.status,
                 metadata=excluded.metadata""",
            (
                ev.experiment_id, md.get("name", "experiment"), ev.timestamp,
                md.get("config_path"), md.get("config_revision"),
                md.get("initial_program"), md.get("evaluator_path"),
                "created", _j(md),
            ),
        )
        if ev.run_id:
            cur.execute(
                """INSERT OR IGNORE INTO runs
                   (run_id, experiment_id, status, iterations_target, output_dir,
                    checkpoint_dir, provenance, metadata)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    ev.run_id, ev.experiment_id, "created",
                    md.get("iterations"), md.get("output_dir"),
                    md.get("checkpoint_dir"), _j(md.get("provenance", {})), _j(md),
                ),
            )
        self._index(cur, "experiment", ev.experiment_id or "", ev.run_id,
                    md.get("name", ""), ev.summary or "")

    def _project_run_status(self, cur: sqlite3.Cursor, ev: Event) -> None:
        status_map = {
            EventType.EXPERIMENT_STARTED: "running",
            EventType.EXPERIMENT_PAUSED: "paused",
            EventType.EXPERIMENT_RESUMED: "running",
            EventType.EXPERIMENT_STOPPED: "stopped",
            EventType.EXPERIMENT_COMPLETED: "completed",
            EventType.EXPERIMENT_FAILED: "failed",
        }
        status = status_map.get(ev.type, "unknown")
        if not ev.run_id:
            return
        # The event log is the source of truth and must replay whatever it
        # happens to contain. A run started by the entrypoint directly — no
        # control API, so no experiment.created event — would otherwise fail the
        # runs→experiments foreign key on replay, and one unreplayable log makes
        # the whole projection a second source of truth rather than a cache.
        experiment_id = ev.experiment_id or f"exp_for_{ev.run_id}"
        cur.execute(
            "INSERT OR IGNORE INTO experiments"
            " (experiment_id, name, created_at, status, metadata)"
            " VALUES (?,?,?,?,?)",
            (experiment_id, ev.metadata.get("name") or experiment_id,
             ev.timestamp, status, "{}"),
        )

        started = ev.timestamp if ev.type == EventType.EXPERIMENT_STARTED else None
        ended = (
            ev.timestamp
            if ev.type in (EventType.EXPERIMENT_STOPPED, EventType.EXPERIMENT_COMPLETED,
                           EventType.EXPERIMENT_FAILED)
            else None
        )
        cur.execute(
            """INSERT INTO runs (run_id, experiment_id, status, started_at, ended_at,
                                 pid, provenance, error, metadata, output_dir)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(run_id) DO UPDATE SET
                 status=excluded.status,
                 started_at=COALESCE(runs.started_at, excluded.started_at),
                 ended_at=COALESCE(excluded.ended_at, runs.ended_at),
                 pid=COALESCE(excluded.pid, runs.pid),
                 provenance=CASE WHEN excluded.provenance != '{}'
                                 THEN excluded.provenance ELSE runs.provenance END,
                 error=COALESCE(excluded.error, runs.error),
                 -- The started event is the one that knows where the run is
                 -- writing, and this column was missing from its insert
                 -- entirely: the value was emitted, carried, and dropped here.
                 -- Every run therefore had a NULL output_dir, which is the one
                 -- field needed to offer "resume this run" as a command.
                 -- COALESCE so a later event without it cannot blank it.
                 output_dir=COALESCE(excluded.output_dir, runs.output_dir)""",
            (
                ev.run_id, experiment_id, status, started, ended, ev.pid,
                _j(ev.metadata.get("provenance", {})),
                _j(ev.error) if ev.error else None, _j(ev.metadata),
                ev.metadata.get("output_dir"),
            ),
        )
        cur.execute(
            "UPDATE experiments SET status=? WHERE experiment_id=?",
            (status, experiment_id),
        )

    def _project_candidate(self, cur: sqlite3.Cursor, ev: Event) -> None:
        md, out = ev.metadata, ev.output
        metrics = ev.metrics or {}
        code = out.get("code")
        code_hash = hashlib.sha256(code.encode()).hexdigest()[:16] if code else md.get("code_hash")
        cur.execute(
            """INSERT INTO candidates
               (run_id, candidate_id, parent_id, generation, iteration, island_id,
                created_at, candidate_type, language, combined_score, metrics,
                complexity, diversity, code_hash, code_length, changes_summary,
                map_elites_cell, eval_status, gen_request_id, gen_provider,
                gen_model, gen_latency_ms, gen_tokens, gen_operator, metadata)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(run_id, candidate_id) DO UPDATE SET
                 parent_id=COALESCE(excluded.parent_id, candidates.parent_id),
                 generation=COALESCE(excluded.generation, candidates.generation),
                 island_id=COALESCE(excluded.island_id, candidates.island_id),
                 changes_summary=COALESCE(excluded.changes_summary, candidates.changes_summary),
                 code_hash=COALESCE(excluded.code_hash, candidates.code_hash),
                 code_length=COALESCE(excluded.code_length, candidates.code_length)""",
            (
                ev.run_id, ev.candidate_id, md.get("parent_id"), ev.generation,
                ev.iteration, ev.island_id, ev.timestamp,
                md.get("candidate_type", "code"), md.get("language", "python"),
                metrics.get("combined_score"), _j(metrics),
                md.get("complexity"), md.get("diversity"), code_hash,
                md.get("code_length", len(code) if code else None),
                _excerpt(md.get("changes_summary"), 2000),
                md.get("map_elites_cell"), "pending",
                md.get("generating_request_id"), md.get("generating_provider"),
                md.get("generating_model"), md.get("generating_latency_ms"),
                md.get("generating_tokens"), md.get("generating_operator"),
                _j(md),
            ),
        )
        # Evaluation happens BEFORE the candidate is added to the database, so
        # its events arrive first and their UPDATE finds no row. Without this
        # backfill every evaluated candidate would sit at "pending" forever.
        prior = cur.execute(
            "SELECT status, combined_score, raw_metrics FROM evaluations"
            " WHERE run_id=? AND candidate_id=? ORDER BY started_at DESC LIMIT 1",
            (ev.run_id, ev.candidate_id),
        ).fetchone()
        if prior is not None:
            cur.execute(
                """UPDATE candidates SET
                     eval_status=?,
                     combined_score=COALESCE(combined_score, ?),
                     metrics=CASE WHEN metrics='{}' THEN ? ELSE metrics END
                   WHERE run_id=? AND candidate_id=?""",
                (
                    prior["status"], prior["combined_score"],
                    prior["raw_metrics"] or "{}", ev.run_id, ev.candidate_id,
                ),
            )
        parents = list(ev.parent_candidate_ids or [])
        if md.get("parent_id") and md["parent_id"] not in parents:
            parents.append(md["parent_id"])
        for p in parents:
            cur.execute(
                "INSERT OR IGNORE INTO candidate_parents(run_id, candidate_id, parent_id, role)"
                " VALUES (?,?,?,?)",
                (ev.run_id, ev.candidate_id, p, "parent"),
            )
        self._index(
            cur, "candidate", ev.candidate_id or "", ev.run_id,
            f"candidate {ev.candidate_id}",
            " ".join(filter(None, [md.get("changes_summary"), _excerpt(code, 8000)])),
        )

    def _project_candidate_metrics(self, cur: sqlite3.Cursor, ev: Event) -> None:
        metrics = ev.metrics or {}
        cur.execute(
            """UPDATE candidates SET
                 combined_score=COALESCE(?, combined_score),
                 metrics=?,
                 complexity=COALESCE(?, complexity),
                 diversity=COALESCE(?, diversity),
                 eval_status=?
               WHERE run_id=? AND candidate_id=?""",
            (
                metrics.get("combined_score"), _j(metrics),
                ev.metadata.get("complexity"), ev.metadata.get("diversity"),
                "ok" if ev.status == Status.OK else ev.status.value,
                ev.run_id, ev.candidate_id,
            ),
        )

    def _project_best(self, cur: sqlite3.Cursor, ev: Event) -> None:
        score = ev.metrics.get("combined_score")
        cur.execute("UPDATE candidates SET is_best=0 WHERE run_id=? AND is_best=1",
                    (ev.run_id,))
        cur.execute("UPDATE candidates SET is_best=1 WHERE run_id=? AND candidate_id=?",
                    (ev.run_id, ev.candidate_id))
        cur.execute(
            "UPDATE runs SET best_candidate_id=?, best_fitness=? WHERE run_id=?",
            (ev.candidate_id, score, ev.run_id),
        )

    def _project_evaluation(self, cur: sqlite3.Cursor, ev: Event) -> None:
        eval_id = ev.metadata.get("evaluation_id") or ev.span_id or ev.event_id
        started = ev.type == EventType.EVALUATOR_STARTED
        cur.execute(
            """INSERT INTO evaluations
               (evaluation_id, run_id, candidate_id, evaluator_id, stage, started_at,
                ended_at, duration_ms, status, exit_code, timed_out, raw_metrics,
                combined_score, failure_class, sandbox_id, stdout_excerpt,
                stderr_excerpt, artifacts, retry_of, metadata)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(evaluation_id) DO UPDATE SET
                 ended_at=COALESCE(excluded.ended_at, evaluations.ended_at),
                 duration_ms=COALESCE(excluded.duration_ms, evaluations.duration_ms),
                 status=excluded.status,
                 exit_code=COALESCE(excluded.exit_code, evaluations.exit_code),
                 timed_out=MAX(evaluations.timed_out, excluded.timed_out),
                 raw_metrics=CASE WHEN excluded.raw_metrics != '{}'
                                  THEN excluded.raw_metrics ELSE evaluations.raw_metrics END,
                 combined_score=COALESCE(excluded.combined_score, evaluations.combined_score),
                 failure_class=COALESCE(excluded.failure_class, evaluations.failure_class),
                 stdout_excerpt=COALESCE(excluded.stdout_excerpt, evaluations.stdout_excerpt),
                 stderr_excerpt=COALESCE(excluded.stderr_excerpt, evaluations.stderr_excerpt),
                 artifacts=CASE WHEN excluded.artifacts != '{}'
                                THEN excluded.artifacts ELSE evaluations.artifacts END""",
            (
                eval_id, ev.run_id, ev.candidate_id,
                ev.metadata.get("evaluator_id"), ev.metadata.get("stage", 0),
                ev.timestamp if started else ev.metadata.get("started_at"),
                None if started else ev.timestamp,
                ev.duration_ms,
                "running" if started else ("ok" if ev.type == EventType.EVALUATOR_COMPLETED else "failed"),
                ev.metadata.get("exit_code"),
                1 if ev.status == Status.TIMEOUT else 0,
                _j(ev.metrics or {}), (ev.metrics or {}).get("combined_score"),
                ev.metadata.get("failure_class") or (ev.error or {}).get("type"),
                ev.metadata.get("sandbox_id"),
                _excerpt(ev.output.get("stdout")), _excerpt(ev.output.get("stderr")),
                _j(ev.metadata.get("artifacts", {})), ev.metadata.get("retry_of"),
                _j(ev.metadata),
            ),
        )
        if ev.type == EventType.EVALUATOR_FAILED and ev.run_id and ev.island_id is not None:
            cur.execute(
                "UPDATE islands SET eval_failures = COALESCE(eval_failures,0)+1"
                " WHERE run_id=? AND island_id=?",
                (ev.run_id, ev.island_id),
            )

    def _project_model_request(self, cur: sqlite3.Cursor, ev: Event) -> None:
        md, m = ev.metadata, ev.metrics or {}
        req_id = md.get("request_id") or ev.span_id or ev.event_id
        failed = ev.type == EventType.MODEL_REQUEST_FAILED
        rate_limited = 1 if ev.type == EventType.MODEL_RATE_LIMITED else int(md.get("rate_limited", 0))
        latency = ev.duration_ms
        total_tokens = m.get("total_tokens")
        tps = None
        if latency and total_tokens and latency > 0:
            tps = round(total_tokens / (latency / 1000.0), 2)
        cur.execute(
            """INSERT INTO model_requests
               (request_id, run_id, candidate_id, generation, iteration, role, provider,
                model, api_base, started_at, ended_at, latency_ms, ttft_ms,
                prompt_tokens, completion_tokens, total_tokens, tokens_per_sec,
                context_limit, status, http_status, rate_limited, retries, retry_of,
                stop_reason, estimated_cost, cost_basis, error, prompt_excerpt,
                response_excerpt, params, metadata)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(request_id) DO UPDATE SET
                 status=excluded.status, latency_ms=excluded.latency_ms,
                 total_tokens=excluded.total_tokens, rate_limited=excluded.rate_limited,
                 retries=excluded.retries, error=excluded.error,
                 -- The started event can only name what was *asked for*; a
                 -- request through the broker is asked for by alias and the
                 -- serving route is known only once it completes. Letting the
                 -- later event win is what keeps provider/model the route that
                 -- did the work rather than the alias that stood in for it.
                 provider=excluded.provider, model=excluded.model,
                 stop_reason=excluded.stop_reason,
                 prompt_tokens=excluded.prompt_tokens,
                 completion_tokens=excluded.completion_tokens,
                 tokens_per_sec=excluded.tokens_per_sec,
                 response_excerpt=excluded.response_excerpt,
                 metadata=excluded.metadata""",
            (
                req_id, ev.run_id, ev.candidate_id, ev.generation, ev.iteration,
                md.get("role"), md.get("provider"), md.get("model"), md.get("api_base"),
                md.get("started_at", ev.timestamp - (latency or 0) / 1000.0),
                ev.timestamp, latency, m.get("ttft_ms"),
                m.get("prompt_tokens"), m.get("completion_tokens"), total_tokens, tps,
                md.get("context_limit"),
                "failed" if failed else ("rate_limited" if rate_limited else "ok"),
                md.get("http_status"), rate_limited, int(md.get("retries", 0)),
                md.get("retry_of"), md.get("stop_reason"),
                m.get("estimated_cost"), md.get("cost_basis"),
                _j(ev.error) if ev.error else None,
                _excerpt(ev.input.get("prompt")), _excerpt(ev.output.get("response")),
                _j(md.get("params", {})), _j(md),
            ),
        )
        if ev.run_id and ev.island_id is not None:
            cur.execute(
                "UPDATE islands SET model_calls=COALESCE(model_calls,0)+1,"
                " tokens=COALESCE(tokens,0)+? WHERE run_id=? AND island_id=?",
                (int(total_tokens or 0), ev.run_id, ev.island_id),
            )

    def _project_island(self, cur: sqlite3.Cursor, ev: Event) -> None:
        m, md = ev.metrics or {}, ev.metadata
        cur.execute(
            """INSERT INTO islands
               (run_id, island_id, updated_at, population, best_score, median_score,
                diversity, generation, stagnation_generations, best_candidate_id, metadata)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(run_id, island_id) DO UPDATE SET
                 updated_at=excluded.updated_at,
                 population=COALESCE(excluded.population, islands.population),
                 best_score=COALESCE(excluded.best_score, islands.best_score),
                 median_score=COALESCE(excluded.median_score, islands.median_score),
                 diversity=COALESCE(excluded.diversity, islands.diversity),
                 generation=COALESCE(excluded.generation, islands.generation),
                 stagnation_generations=COALESCE(excluded.stagnation_generations,
                                                 islands.stagnation_generations),
                 best_candidate_id=COALESCE(excluded.best_candidate_id,
                                            islands.best_candidate_id),
                 metadata=excluded.metadata""",
            (
                ev.run_id, ev.island_id, ev.timestamp,
                m.get("population"), m.get("best_score"), m.get("median_score"),
                m.get("diversity"), ev.generation, m.get("stagnation_generations"),
                md.get("best_candidate_id"), _j(md),
            ),
        )

    def _project_migration(self, cur: sqlite3.Cursor, ev: Event) -> None:
        for mig in ev.metadata.get("migrations", []):
            mid = mig.get("migration_id") or f"{ev.event_id}:{mig.get('candidate_id')}:{mig.get('target_island')}"
            cur.execute(
                """INSERT OR IGNORE INTO migrations
                   (migration_id, run_id, generation, timestamp, source_island,
                    target_island, candidate_id, new_candidate_id, metadata)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    mid, ev.run_id, ev.generation, ev.timestamp,
                    mig.get("source_island"), mig.get("target_island"),
                    mig.get("candidate_id"), mig.get("new_candidate_id"), _j(mig),
                ),
            )
            if mig.get("source_island") is not None:
                cur.execute(
                    "UPDATE islands SET migrants_sent=COALESCE(migrants_sent,0)+1"
                    " WHERE run_id=? AND island_id=?",
                    (ev.run_id, mig["source_island"]),
                )
            if mig.get("target_island") is not None:
                cur.execute(
                    "UPDATE islands SET migrants_received=COALESCE(migrants_received,0)+1"
                    " WHERE run_id=? AND island_id=?",
                    (ev.run_id, mig["target_island"]),
                )

    def _project_map_elites(self, cur: sqlite3.Cursor, ev: Event) -> None:
        md, m = ev.metadata, ev.metrics or {}
        cell_key = md.get("cell_key")
        if not cell_key:
            return
        # island_id is part of the identity: each island has its own feature map.
        island_id = ev.island_id if ev.island_id is not None else -1
        prev = cur.execute(
            "SELECT candidate_id, score FROM map_elites_cells"
            " WHERE run_id=? AND island_id=? AND cell_key=?",
            (ev.run_id, island_id, cell_key),
        ).fetchone()
        cur.execute(
            """INSERT INTO map_elites_cells
               (run_id, island_id, cell_key, coords, dimensions, candidate_id, score,
                updated_at, generation, replacements)
               VALUES (?,?,?,?,?,?,?,?,?,0)
               ON CONFLICT(run_id, island_id, cell_key) DO UPDATE SET
                 candidate_id=excluded.candidate_id, score=excluded.score,
                 updated_at=excluded.updated_at, generation=excluded.generation,
                 replacements=map_elites_cells.replacements+1""",
            (
                ev.run_id, island_id, cell_key, _j(md.get("coords", [])),
                _j(md.get("dimensions", [])), ev.candidate_id,
                m.get("score"), ev.timestamp, ev.generation,
            ),
        )
        cur.execute(
            """INSERT INTO map_elites_history
               (run_id, island_id, cell_key, candidate_id, previous_candidate_id, score,
                previous_score, generation, iteration, timestamp)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                ev.run_id, island_id, cell_key, ev.candidate_id,
                prev["candidate_id"] if prev else None, m.get("score"),
                prev["score"] if prev else None, ev.generation, ev.iteration,
                ev.timestamp,
            ),
        )
        cur.execute(
            "UPDATE candidates SET map_elites_cell=? WHERE run_id=? AND candidate_id=?",
            (cell_key, ev.run_id, ev.candidate_id),
        )

    def _project_checkpoint(self, cur: sqlite3.Cursor, ev: Event) -> None:
        md, m = ev.metadata, ev.metrics or {}
        cid = md.get("checkpoint_id") or f"{ev.run_id}:{ev.iteration}"
        cur.execute(
            """INSERT INTO checkpoints
               (checkpoint_id, run_id, iteration, path, created_at, size_bytes,
                num_programs, best_score, status, metadata)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(checkpoint_id) DO UPDATE SET
                 size_bytes=excluded.size_bytes, num_programs=excluded.num_programs,
                 best_score=excluded.best_score, status=excluded.status""",
            (
                cid, ev.run_id, ev.iteration, md.get("path"), ev.timestamp,
                int(m.get("size_bytes", 0)), int(m.get("num_programs", 0)),
                m.get("best_score"), "ok", _j(md),
            ),
        )
        cur.execute("UPDATE runs SET checkpoint_dir=COALESCE(?, checkpoint_dir) WHERE run_id=?",
                    (md.get("path"), ev.run_id))

    def _project_sandbox(self, cur: sqlite3.Cursor, ev: Event) -> None:
        md = ev.metadata
        sid = md.get("sandbox_id") or ev.span_id or ev.event_id
        creating = ev.type == EventType.SANDBOX_CREATED
        done = ev.type in (EventType.SANDBOX_COMPLETED, EventType.SANDBOX_DESTROYED)
        cur.execute(
            """INSERT INTO sandbox_runs
               (sandbox_id, run_id, candidate_id, backend, mode, image, workdir,
                home_dir, started_at, ended_at, status, exit_code, cpu_limit,
                mem_limit_mb, timeout_s, network_policy, termination_reason,
                isolation, metadata)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(sandbox_id) DO UPDATE SET
                 ended_at=COALESCE(excluded.ended_at, sandbox_runs.ended_at),
                 status=excluded.status,
                 exit_code=COALESCE(excluded.exit_code, sandbox_runs.exit_code),
                 termination_reason=COALESCE(excluded.termination_reason,
                                             sandbox_runs.termination_reason)""",
            (
                sid, ev.run_id, ev.candidate_id, md.get("backend"), md.get("mode"),
                md.get("image"), md.get("workdir"), md.get("home_dir"),
                ev.timestamp if creating else md.get("started_at"),
                ev.timestamp if done else None,
                ev.status.value, md.get("exit_code"), md.get("cpu_limit"),
                md.get("mem_limit_mb"), md.get("timeout_s"),
                md.get("network_policy"), md.get("termination_reason"),
                _j(md.get("isolation", {})), _j(md),
            ),
        )

    def _project_agent_run(self, cur: sqlite3.Cursor, ev: Event) -> None:
        md = ev.metadata
        aid = md.get("agent_run_id") or ev.span_id or ev.event_id
        started = ev.type in (EventType.OPENCODE_AGENT_STARTED, EventType.OMO_MEMBER_STARTED)
        harness = "omo" if ev.type.value.startswith("omo.") else "opencode"
        cur.execute(
            """INSERT INTO agent_runs
               (agent_run_id, sandbox_id, run_id, candidate_id, harness, agent, mode,
                model, started_at, ended_at, status, tool_calls, tokens, metadata)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(agent_run_id) DO UPDATE SET
                 ended_at=COALESCE(excluded.ended_at, agent_runs.ended_at),
                 status=excluded.status,
                 tool_calls=MAX(agent_runs.tool_calls, excluded.tool_calls),
                 tokens=MAX(agent_runs.tokens, excluded.tokens)""",
            (
                aid, md.get("sandbox_id"), ev.run_id, ev.candidate_id, harness,
                md.get("agent"), md.get("mode"), md.get("model"),
                ev.timestamp if started else md.get("started_at"),
                None if started else ev.timestamp,
                ev.status.value, int(md.get("tool_calls", 0)),
                int((ev.metrics or {}).get("tokens", 0)), _j(md),
            ),
        )

    def _index(self, cur: sqlite3.Cursor, entity_type: str, entity_id: str,
               run_id: Optional[str], title: str, body: str) -> None:
        if not entity_id:
            return
        cur.execute(
            "INSERT INTO search_index(entity_type, entity_id, run_id, title, body)"
            " VALUES (?,?,?,?,?)",
            (entity_type, entity_id, run_id, title or "", (body or "")[:20000]),
        )

    # -- direct writes (control plane owned entities) --------------------

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self._write_lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        cur = self.reader().execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # -- maintenance -----------------------------------------------------

    def reconcile_orphaned_runs(self) -> List[Dict[str, Any]]:
        """
        Mark runs whose process is gone but whose status still says "running".

        A run killed by a crash, a container restart or a `kill -9` never emits
        its own stopped event, so the projection keeps reporting it as running
        — forever, and in the Control Center's run list. That is precisely the
        plausible-looking wrong value this project treats as worse than a
        blank: the operator reads "running" and waits for output that will
        never come.

        Only *this machine's* runs can be judged, and only when a PID is
        recorded. Anything unknowable is left alone rather than guessed at.
        """
        rows = self.query(
            "SELECT run_id, pid, started_at FROM runs "
            " WHERE status IN ('running', 'created') AND pid IS NOT NULL")
        reconciled: List[Dict[str, Any]] = []
        for row in rows:
            if _process_alive(int(row["pid"])):
                continue
            with self._write_lock:
                self._conn.execute(
                    "UPDATE runs SET status='failed', ended_at=COALESCE(ended_at, ?),"
                    "                error=COALESCE(error, ?)"
                    " WHERE run_id=? AND status IN ('running','created')",
                    (time.time(),
                     _j({"type": "orphaned",
                         "message": "the engine process is gone and the run never "
                                    "reported an end; marked failed on reconnect"}),
                     row["run_id"]),
                )
                self._conn.commit()
            logger.warning("Run %s was marked running but pid %s is gone; "
                           "recorded as failed", row["run_id"], row["pid"])
            reconciled.append({"run_id": row["run_id"], "pid": row["pid"]})
        return reconciled

    def rebuild_projections_from_log(self, ndjson_path: str) -> int:
        """
        Replay the durable event log to reconstruct every projection.

        This is what makes projections a cache rather than a second source of
        truth, and is the recovery path if the DB is lost or a projection bug
        is fixed retroactively.
        """
        with self._write_lock:
            cur = self._conn.cursor()
            for table in (
                "candidates", "candidate_parents", "evaluations", "model_requests",
                "islands", "migrations", "map_elites_cells", "map_elites_history",
                "checkpoints", "sandbox_runs", "agent_runs", "resource_metrics",
                "runs", "experiments",
            ):
                cur.execute(f"DELETE FROM {table}")
            cur.execute("DELETE FROM search_index")
            cur.execute("DELETE FROM events")
            self._conn.commit()

        count = 0
        batch: List[Event] = []
        with open(ndjson_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    batch.append(Event.from_dict(json.loads(line)))
                except Exception:
                    continue  # a torn final line from a killed process
                if len(batch) >= 500:
                    count += self.ingest(batch)
                    batch = []
        if batch:
            count += self.ingest(batch)
        return count

    def prune_events(self, keep_days: float = 30.0) -> int:
        cutoff = time.time() - keep_days * 86400
        with self._write_lock:
            cur = self._conn.execute(
                "DELETE FROM events WHERE timestamp < ? AND type IN "
                "('resource.cpu','resource.ram','resource.disk','resource.network',"
                " 'evaluator.stdout','evaluator.stderr','telemetry.health')",
                (cutoff,),
            )
            self._conn.commit()
            return cur.rowcount
