"""
Evolution control-plane API.

Three surfaces, matching SOURCE_OF_TRUTH section 24:

  /api/control/*   mutating operations on runs (start/stop/checkpoint/resume)
  /api/query/*     indexed reads for every UI view
  /api/stream      Server-Sent Events; the browser never polls whole state

Every endpoint reads from the event-derived store or the live process table.
There is no endpoint that synthesises a value for display.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import time
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..providers.doctor import ProviderDoctor, apply_reports
from ..providers.profiles import Role
from ..providers.router import ModelRouter
from ..runner.manager import RunManager, RunSpec
from ..storage.store import Store
from ..telemetry.bus import configure_bus, get_bus
from ..telemetry.collector import EventCollector
from ..telemetry.events import Component, Event, EventType, Status

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class AppState:
    """Wiring shared by every route."""

    def __init__(self, workspace: Optional[str] = None) -> None:
        self.workspace = os.path.abspath(
            workspace or os.environ.get("EVOLUTION_WORKSPACE",
                                        os.path.join(_ROOT, ".evolution", "workspace"))
        )
        os.makedirs(self.workspace, exist_ok=True)
        self.store = Store(os.path.join(self.workspace, "control_plane.db"))
        self.collector = EventCollector(self.store)
        self.collector_port = self.collector.serve_socket(
            port=int(os.environ.get("EVOLUTION_COLLECTOR_PORT", "0"))
        )
        # A run whose process died with the container never emitted its own
        # stopped event, and would otherwise be reported as running forever.
        self.orphaned_runs = self.store.reconcile_orphaned_runs()
        self.runner = RunManager(self.workspace, collector_port=self.collector_port)
        self.router = ModelRouter()
        self.doctor = ProviderDoctor()
        self.started_at = time.time()
        self.last_doctor_reports: List[Dict[str, Any]] = []

        # The API's own events (control commands, doctor results) go through the
        # same bus and collector as the engine's, so the audit trail is unified.
        bus = configure_bus(ndjson_path=os.path.join(self.workspace, "control_plane.ndjson"))
        bus.add_sink(_CollectorSink(self.collector))

    def attach_run_log(self, event_log: str) -> None:
        self.collector.tail_log(event_log, from_start=True)


class _CollectorSink:
    name = "collector"

    def __init__(self, collector: EventCollector) -> None:
        self._collector = collector

    def write(self, events: List[Event]) -> None:
        self._collector.submit(list(events))

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------
# request models
# --------------------------------------------------------------------------


class StartRunRequest(BaseModel):
    initial_program: str
    evaluator: str
    config_path: Optional[str] = None
    iterations: Optional[int] = Field(None, ge=1)
    target_score: Optional[float] = None
    checkpoint: Optional[str] = None
    name: str = "experiment"
    env: Dict[str, str] = Field(default_factory=dict)


class StopRunRequest(BaseModel):
    force: bool = False
    timeout: float = 30.0


class ResumeRequest(BaseModel):
    checkpoint: Optional[str] = None
    iterations: Optional[int] = None


class ForceRouteRequest(BaseModel):
    role: str
    profile_id: str


def create_app(workspace: Optional[str] = None) -> FastAPI:
    state = AppState(workspace)
    app = FastAPI(title="Evolution Control Plane", version="1.0.0")
    app.state.evolution = state

    app.add_middleware(
        CORSMiddleware,
        # Dev-only convenience: the Vite dev server runs on another port. In a
        # packaged build the UI is served from this same origin.
        allow_origins=[
            "http://localhost:5173", "http://127.0.0.1:5173",
            "http://localhost:8000", "http://127.0.0.1:8000",
        ],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ------------------------------------------------------------ health
    @app.get("/api/health")
    def health() -> Dict[str, Any]:
        bus = get_bus()
        return {
            "status": "ok",
            "uptime_s": time.time() - state.started_at,
            "workspace": state.workspace,
            "collector_port": state.collector_port,
            "collector": state.collector.health(),
            "telemetry_bus": bus.stats.to_dict() if bus else None,
            "active_runs": sum(1 for r in state.runner.runs.values() if r.is_alive()),
            "capabilities": state.runner.capabilities(),
        }

    @app.get("/api/system")
    def system() -> Dict[str, Any]:
        """Real host metrics; absent values are reported as null, never zero."""
        info: Dict[str, Any] = {
            "workspace": state.workspace,
            "collector": state.collector.health(),
            "runner_capabilities": state.runner.capabilities(),
            "opencode_isolation": _opencode_isolation_status(state),
        }
        bus = get_bus()
        info["telemetry_bus"] = bus.stats.to_dict() if bus else None
        try:
            import psutil

            vm = psutil.virtual_memory()
            du = psutil.disk_usage(state.workspace)
            info["host"] = {
                "cpu_percent": psutil.cpu_percent(interval=0.1),
                "cpu_count": psutil.cpu_count(),
                "ram_total_mb": round(vm.total / 1048576, 1),
                "ram_used_mb": round(vm.used / 1048576, 1),
                "ram_percent": vm.percent,
                "disk_total_gb": round(du.total / 1073741824, 2),
                "disk_free_gb": round(du.free / 1073741824, 2),
                "disk_percent": du.percent,
            }
        except Exception as exc:
            info["host"] = None
            info["host_error"] = f"psutil unavailable: {exc}"
        try:
            with open(os.path.join(_ROOT, "UPSTREAM.json")) as fh:
                info["upstream"] = json.load(fh)
        except Exception:
            info["upstream"] = None
        return info

    # ----------------------------------------------------------- control
    @app.get("/api/control/capabilities")
    def capabilities() -> Dict[str, Any]:
        return state.runner.capabilities()

    @app.post("/api/control/runs")
    def start_run(req: StartRunRequest) -> Dict[str, Any]:
        spec = RunSpec(
            initial_program=req.initial_program, evaluator=req.evaluator,
            config_path=req.config_path, iterations=req.iterations,
            target_score=req.target_score, checkpoint=req.checkpoint,
            name=req.name, env=req.env,
        )
        problems = spec.validate()
        if problems:
            raise HTTPException(status_code=400, detail={"errors": problems})
        try:
            run = state.runner.start(spec)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        state.attach_run_log(run.event_log)
        return run.to_dict()

    @app.post("/api/control/runs/{run_id}/stop")
    def stop_run(run_id: str, req: StopRunRequest) -> Dict[str, Any]:
        try:
            return state.runner.stop(run_id, force=req.force, timeout=req.timeout)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown run {run_id}")

    @app.post("/api/control/runs/{run_id}/checkpoint")
    def checkpoint_now(run_id: str) -> Dict[str, Any]:
        try:
            return state.runner.checkpoint_now(run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown run {run_id}")
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.post("/api/control/runs/{run_id}/resume")
    def resume_run(run_id: str, req: ResumeRequest) -> Dict[str, Any]:
        try:
            run = state.runner.resume(run_id, req.checkpoint, req.iterations)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown run {run_id}")
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        state.attach_run_log(run.event_log)
        return run.to_dict()

    @app.post("/api/control/runs/{run_id}/clone")
    def clone_run(run_id: str, req: ResumeRequest) -> Dict[str, Any]:
        try:
            run = state.runner.clone(run_id, iterations=req.iterations)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown run {run_id}")
        state.attach_run_log(run.event_log)
        return run.to_dict()

    @app.delete("/api/control/runs/{run_id}/checkpoints/{iteration}")
    def delete_checkpoint(run_id: str, iteration: int) -> Dict[str, Any]:
        try:
            ok = state.runner.delete_checkpoint(run_id, iteration)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown run {run_id}")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not ok:
            raise HTTPException(status_code=404, detail="checkpoint not found")
        return {"deleted": True, "run_id": run_id, "iteration": iteration}

    # ------------------------------------------------------------- query
    @app.get("/api/query/runs")
    def list_runs() -> Dict[str, Any]:
        live = {r["run_id"]: r for r in state.runner.list_runs()}
        rows = state.store.query(
            "SELECT * FROM runs ORDER BY COALESCE(started_at, 0) DESC LIMIT 500"
        )
        for row in rows:
            row["provenance"] = json.loads(row.get("provenance") or "{}")
            row["metadata"] = json.loads(row.get("metadata") or "{}")
            row["live"] = live.get(row["run_id"])
        # Runs that were started but have not emitted yet still belong in the list.
        known = {r["run_id"] for r in rows}
        for rid, lr in live.items():
            if rid not in known:
                rows.append({"run_id": rid, "status": lr["status"], "live": lr,
                             "experiment_id": lr["experiment_id"]})
        return {"runs": rows}

    @app.get("/api/query/runs/{run_id}")
    def get_run(run_id: str) -> Dict[str, Any]:
        row = state.store.query_one("SELECT * FROM runs WHERE run_id=?", (run_id,))
        live = state.runner.get(run_id)
        if row is None and live is None:
            raise HTTPException(status_code=404, detail=f"unknown run {run_id}")
        row = row or {"run_id": run_id}
        row["provenance"] = json.loads(row.get("provenance") or "{}")
        row["metadata"] = json.loads(row.get("metadata") or "{}")
        row["live"] = live.to_dict() if live else None
        row["counts"] = {
            "candidates": _count(state, "candidates", run_id),
            "evaluations": _count(state, "evaluations", run_id),
            "model_requests": _count(state, "model_requests", run_id),
            "events": _count(state, "events", run_id),
            "checkpoints": _count(state, "checkpoints", run_id),
        }
        return row

    @app.get("/api/query/runs/{run_id}/summary")
    def run_summary(run_id: str) -> Dict[str, Any]:
        """Powers the Overview header. Every field is an aggregate over events."""
        best = state.store.query_one(
            "SELECT candidate_id, combined_score, generation FROM candidates"
            " WHERE run_id=? ORDER BY combined_score DESC LIMIT 1", (run_id,))
        gen = state.store.query_one(
            "SELECT MAX(generation) g, MAX(iteration) i FROM candidates WHERE run_id=?",
            (run_id,))
        tokens = state.store.query_one(
            "SELECT COALESCE(SUM(total_tokens),0) t, COUNT(*) n FROM model_requests"
            " WHERE run_id=?", (run_id,))
        cells = state.store.query_one(
            "SELECT COUNT(*) occupied FROM map_elites_cells WHERE run_id=?", (run_id,))
        islands = state.store.query("SELECT * FROM islands WHERE run_id=? ORDER BY island_id",
                                    (run_id,))
        evals = state.store.query_one(
            "SELECT COUNT(*) total, SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed"
            " FROM evaluations WHERE run_id=?", (run_id,))
        providers = state.store.query(
            "SELECT provider, model, COUNT(*) requests, COALESCE(SUM(total_tokens),0) tokens,"
            " AVG(latency_ms) avg_latency, SUM(rate_limited) rate_limited"
            " FROM model_requests WHERE run_id=? GROUP BY provider, model"
            " ORDER BY requests DESC", (run_id,))
        return {
            "run_id": run_id,
            "best": best,
            "generation": (gen or {}).get("g"),
            "iteration": (gen or {}).get("i"),
            "candidates": _count(state, "candidates", run_id),
            "model_requests": (tokens or {}).get("n", 0),
            "tokens": (tokens or {}).get("t", 0),
            "map_elites_occupied": (cells or {}).get("occupied", 0),
            "islands": islands,
            "evaluations": evals,
            "providers": providers,
        }

    @app.get("/api/query/runs/{run_id}/candidates")
    def list_candidates(
        run_id: str,
        limit: int = Query(200, le=2000),
        offset: int = 0,
        island: Optional[int] = None,
        generation: Optional[int] = None,
        min_score: Optional[float] = None,
        order: str = Query("score", pattern="^(score|generation|iteration|recent)$"),
    ) -> Dict[str, Any]:
        where = ["run_id = ?"]
        params: List[Any] = [run_id]
        if island is not None:
            where.append("island_id = ?")
            params.append(island)
        if generation is not None:
            where.append("generation = ?")
            params.append(generation)
        if min_score is not None:
            where.append("combined_score >= ?")
            params.append(min_score)
        order_sql = {
            "score": "combined_score DESC",
            "generation": "generation DESC",
            "iteration": "iteration DESC",
            "recent": "created_at DESC",
        }[order]
        clause = " AND ".join(where)
        total = state.store.query_one(
            f"SELECT COUNT(*) c FROM candidates WHERE {clause}", params)["c"]
        rows = state.store.query(
            f"SELECT * FROM candidates WHERE {clause} ORDER BY {order_sql} "
            f"LIMIT ? OFFSET ?", params + [limit, offset])
        for r in rows:
            r["metrics"] = json.loads(r.get("metrics") or "{}")
            r["metadata"] = json.loads(r.get("metadata") or "{}")
        return {"candidates": rows, "total": total, "limit": limit, "offset": offset}

    @app.get("/api/query/runs/{run_id}/candidates/{candidate_id}")
    def get_candidate(run_id: str, candidate_id: str) -> Dict[str, Any]:
        row = state.store.query_one(
            "SELECT * FROM candidates WHERE run_id=? AND candidate_id=?",
            (run_id, candidate_id))
        if row is None:
            raise HTTPException(status_code=404, detail="unknown candidate")
        row["metrics"] = json.loads(row.get("metrics") or "{}")
        row["metadata"] = json.loads(row.get("metadata") or "{}")
        row["parents"] = state.store.query(
            "SELECT parent_id, role FROM candidate_parents WHERE run_id=? AND candidate_id=?",
            (run_id, candidate_id))
        row["children"] = state.store.query(
            "SELECT candidate_id FROM candidate_parents WHERE run_id=? AND parent_id=?",
            (run_id, candidate_id))
        row["evaluations"] = state.store.query(
            "SELECT * FROM evaluations WHERE run_id=? AND candidate_id=?"
            " ORDER BY started_at", (run_id, candidate_id))
        for e in row["evaluations"]:
            e["raw_metrics"] = json.loads(e.get("raw_metrics") or "{}")
        # Joined on `gen_request_id`, not on `model_requests.candidate_id`.
        # Upstream never sets a candidate id on a request — measured: 0 of 22 —
        # so the old join returned an empty list for every candidate in every
        # run. The generating request is knowable now that attribution crosses
        # the process boundary, and this is where an operator looks for it.
        row["model_requests"] = state.store.query(
            "SELECT request_id, provider, model, role, latency_ms, total_tokens,"
            "       status, rate_limited, started_at, stop_reason"
            "  FROM model_requests"
            " WHERE run_id = ? AND (candidate_id = ? OR request_id = ?)"
            " ORDER BY started_at",
            (run_id, candidate_id, row.get("gen_request_id")))

        # Verification is emitted as events, not projected into a table: it is
        # rare (champions and outliers only) and its payload is a report rather
        # than a row.
        row["verification"] = [
            {"type": r["type"], "status": r["status"], "summary": r["summary"],
             "timestamp": r["timestamp"],
             **{k: v for k, v in (json.loads(r["payload"]).get("metadata") or {}).items()
                if k in ("trigger", "passed", "failures", "errors", "checks_run",
                         "spec_declared", "reason", "score")}}
            for r in state.store.query(
                "SELECT type, status, summary, timestamp, payload FROM events"
                " WHERE run_id = ? AND candidate_id = ?"
                "   AND type IN (?, ?, ?)"
                " ORDER BY seq",
                (run_id, candidate_id,
                 EventType.CANDIDATE_VERIFICATION_PASSED.value,
                 EventType.CANDIDATE_VERIFICATION_FAILED.value,
                 EventType.CANDIDATE_SUSPICIOUS.value))
        ]
        # Code lives in the event log, not duplicated into projections.
        code_ev = state.store.query_one(
            "SELECT payload FROM events WHERE run_id=? AND candidate_id=? AND type=?"
            " ORDER BY seq LIMIT 1",
            (run_id, candidate_id, EventType.CANDIDATE_CREATED.value))
        row["code"] = None
        if code_ev:
            row["code"] = (json.loads(code_ev["payload"]).get("output") or {}).get("code")
        return row

    @app.get("/api/query/runs/{run_id}/lineage")
    def lineage(run_id: str, limit: int = Query(3000, le=20000)) -> Dict[str, Any]:
        """
        Graph payload for the Evolution view.

        Server-side capped and score-ordered: the client must never receive an
        unbounded node set (section 25).
        """
        nodes = state.store.query(
            "SELECT candidate_id, parent_id, generation, iteration, island_id,"
            " combined_score, is_best, map_elites_cell, eval_status, created_at"
            " FROM candidates WHERE run_id=? ORDER BY COALESCE(iteration, 0) LIMIT ?",
            (run_id, limit))
        ids = {n["candidate_id"] for n in nodes}
        edges = [
            e for e in state.store.query(
                "SELECT candidate_id, parent_id FROM candidate_parents WHERE run_id=?",
                (run_id,))
            if e["candidate_id"] in ids and e["parent_id"] in ids
        ]
        total = _count(state, "candidates", run_id)
        return {
            "nodes": nodes, "edges": edges, "total": total,
            "truncated": total > len(nodes),
        }

    @app.get("/api/query/runs/{run_id}/map-elites")
    def map_elites(run_id: str, island: Optional[int] = None,
                   generation: Optional[int] = None) -> Dict[str, Any]:
        if generation is not None:
            # Historical reconstruction for the generation scrubber: the last
            # occupant recorded at or before the requested generation.
            rows = state.store.query(
                """SELECT h.cell_key, h.island_id, h.candidate_id, h.score, h.generation
                   FROM map_elites_history h
                   JOIN (SELECT cell_key, island_id, MAX(id) mid
                         FROM map_elites_history
                         WHERE run_id=? AND COALESCE(generation, 0) <= ?
                         GROUP BY cell_key, island_id) last
                     ON h.cell_key = last.cell_key AND h.island_id = last.island_id
                        AND h.id = last.mid
                   WHERE h.run_id=?""",
                (run_id, generation, run_id))
        else:
            sql = "SELECT * FROM map_elites_cells WHERE run_id=?"
            params: List[Any] = [run_id]
            if island is not None:
                sql += " AND island_id=?"
                params.append(island)
            rows = state.store.query(sql, params)
            for r in rows:
                r["coords"] = json.loads(r.get("coords") or "[]")
                r["dimensions"] = json.loads(r.get("dimensions") or "[]")
        if island is not None:
            rows = [r for r in rows if r.get("island_id") == island]
        dims_row = state.store.query_one(
            "SELECT dimensions FROM map_elites_cells WHERE run_id=? LIMIT 1", (run_id,))
        return {
            "cells": rows,
            "dimensions": json.loads(dims_row["dimensions"]) if dims_row else [],
            "generation": generation,
            "island": island,
            "max_generation": (state.store.query_one(
                "SELECT MAX(generation) g FROM map_elites_history WHERE run_id=?",
                (run_id,)) or {}).get("g"),
        }

    @app.get("/api/query/runs/{run_id}/islands")
    def islands(run_id: str) -> Dict[str, Any]:
        rows = state.store.query(
            "SELECT * FROM islands WHERE run_id=? ORDER BY island_id", (run_id,))
        for r in rows:
            r["metadata"] = json.loads(r.get("metadata") or "{}")
        migrations = state.store.query(
            "SELECT * FROM migrations WHERE run_id=? ORDER BY timestamp DESC LIMIT 500",
            (run_id,))
        return {"islands": rows, "migrations": migrations}

    @app.get("/api/query/runs/{run_id}/model-requests")
    def model_requests(run_id: str, limit: int = Query(200, le=2000),
                       offset: int = 0, provider: Optional[str] = None,
                       status: Optional[str] = None) -> Dict[str, Any]:
        where, params = ["run_id = ?"], [run_id]
        if provider:
            where.append("provider = ?")
            params.append(provider)
        if status:
            where.append("status = ?")
            params.append(status)
        clause = " AND ".join(where)
        total = state.store.query_one(
            f"SELECT COUNT(*) c FROM model_requests WHERE {clause}", params)["c"]
        rows = state.store.query(
            f"SELECT * FROM model_requests WHERE {clause} ORDER BY started_at DESC"
            f" LIMIT ? OFFSET ?", params + [limit, offset])
        for r in rows:
            r["params"] = json.loads(r.get("params") or "{}")
        return {"model_requests": rows, "total": total}

    @app.get("/api/query/runs/{run_id}/route-quality")
    def route_quality(run_id: str, min_attempts: Optional[int] = None,
                      pool: Optional[str] = None) -> Dict[str, Any]:
        """
        Which route produced the better mutations, and at what cost.

        Distinct from `/model-requests`, which answers "did the route respond?"
        A route can be perfectly reliable and return nothing but duplicates.

        `pool` accepts a comma-separated list of additional run ids. A single
        short run rarely clears the minimum attempt count on any route, and
        pooling comparable runs is the honest way to reach it — the response
        names every run that went in, and its attribution coverage, so a reader
        can check they were comparable rather than take it on trust.
        """
        from control_plane.analysis.route_quality import analyse, analyse_runs

        conn = state.store.reader()
        if pool:
            ids = [run_id] + [r.strip() for r in pool.split(",") if r.strip()]
            return analyse_runs(conn, ids, min_attempts=min_attempts)
        return analyse(conn, run_id, min_attempts=min_attempts)

    @app.get("/api/query/runs/{run_id}/evaluations")
    def evaluations(run_id: str, limit: int = Query(200, le=2000),
                    status: Optional[str] = None) -> Dict[str, Any]:
        where, params = ["run_id = ?"], [run_id]
        if status:
            where.append("status = ?")
            params.append(status)
        clause = " AND ".join(where)
        rows = state.store.query(
            f"SELECT * FROM evaluations WHERE {clause} ORDER BY started_at DESC LIMIT ?",
            params + [limit])
        for r in rows:
            r["raw_metrics"] = json.loads(r.get("raw_metrics") or "{}")
        return {
            "evaluations": rows,
            "total": state.store.query_one(
                f"SELECT COUNT(*) c FROM evaluations WHERE {clause}", params)["c"],
        }

    @app.get("/api/query/runs/{run_id}/checkpoints")
    def checkpoints(run_id: str) -> Dict[str, Any]:
        # Disk is authoritative: a checkpoint deleted outside the UI must not
        # keep appearing because a stale row says it exists.
        on_disk = state.runner.list_checkpoints(run_id)
        recorded = {c["checkpoint_id"]: c for c in state.store.query(
            "SELECT * FROM checkpoints WHERE run_id=? ORDER BY iteration", (run_id,))}
        for c in on_disk:
            rec = recorded.get(c["checkpoint_id"])
            if rec:
                c["best_score"] = c.get("best_score") or rec.get("best_score")
                c["num_programs"] = c.get("num_programs") or rec.get("num_programs")
        return {"checkpoints": on_disk}

    @app.get("/api/query/runs/{run_id}/events")
    def events(run_id: str, limit: int = Query(200, le=2000), after_seq: int = 0,
               type: Optional[str] = None, status: Optional[str] = None,
               candidate_id: Optional[str] = None) -> Dict[str, Any]:
        where, params = ["run_id = ?", "seq > ?"], [run_id, after_seq]
        if type:
            where.append("type = ?")
            params.append(type)
        if status:
            where.append("status = ?")
            params.append(status)
        if candidate_id:
            where.append("candidate_id = ?")
            params.append(candidate_id)
        rows = state.store.query(
            f"SELECT seq, event_id, type, component, status, summary, timestamp,"
            f" duration_ms, candidate_id, island_id, generation, iteration, payload"
            f" FROM events WHERE {' AND '.join(where)} ORDER BY seq DESC LIMIT ?",
            params + [limit])
        for r in rows:
            r["payload"] = json.loads(r["payload"])
        return {"events": rows}

    @app.get("/api/query/runs/{run_id}/resources")
    def resources(run_id: str, kind: Optional[str] = None,
                  buckets: int = Query(300, le=2000)) -> Dict[str, Any]:
        """Server-side downsampling: the browser never receives raw sample streams."""
        kinds = [kind] if kind else [
            r["kind"] for r in state.store.query(
                "SELECT DISTINCT kind FROM resource_metrics WHERE run_id=?", (run_id,))
        ]
        series: Dict[str, List[Dict[str, Any]]] = {}
        for k in kinds:
            bounds = state.store.query_one(
                "SELECT MIN(timestamp) lo, MAX(timestamp) hi, COUNT(*) n"
                " FROM resource_metrics WHERE run_id=? AND kind=?", (run_id, k))
            if not bounds or not bounds["n"]:
                series[k] = []
                continue
            lo, hi = bounds["lo"], bounds["hi"]
            width = max((hi - lo) / max(1, buckets), 1e-6)
            series[k] = state.store.query(
                "SELECT CAST((timestamp - ?) / ? AS INTEGER) bucket,"
                " AVG(value) avg, MIN(value) min, MAX(value) max,"
                " MIN(timestamp) t, COUNT(*) n"
                " FROM resource_metrics WHERE run_id=? AND kind=?"
                " GROUP BY bucket ORDER BY bucket", (lo, width, run_id, k))
        return {"series": series}

    @app.get("/api/query/runs/{run_id}/logs")
    def logs(run_id: str, stream: str = "stdout", lines: int = Query(300, le=5000)) -> Dict[str, Any]:
        try:
            return {"stream": stream, "text": state.runner.log_tail(run_id, stream, lines)}
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown run {run_id}")

    @app.get("/api/query/search")
    def search(q: str, run_id: Optional[str] = None,
               limit: int = Query(50, le=500)) -> Dict[str, Any]:
        """Global command-palette search over the FTS index."""
        if not q.strip():
            return {"results": []}
        sql = ("SELECT entity_type, entity_id, run_id, title,"
               " snippet(search_index, 4, '«', '»', '…', 12) excerpt"
               " FROM search_index WHERE search_index MATCH ?")
        params: List[Any] = [q]
        if run_id:
            sql += " AND run_id = ?"
            params.append(run_id)
        sql += " LIMIT ?"
        params.append(limit)
        try:
            return {"results": state.store.query(sql, params)}
        except Exception as exc:
            # A malformed FTS expression is user error, not a server fault.
            raise HTTPException(status_code=400, detail=f"invalid search query: {exc}")

    @app.get("/api/query/compare")
    def compare(run_ids: str) -> Dict[str, Any]:
        ids = [r.strip() for r in run_ids.split(",") if r.strip()]
        if not 2 <= len(ids) <= 6:
            raise HTTPException(status_code=400, detail="provide 2-6 run_ids")
        out = []
        for rid in ids:
            row = state.store.query_one("SELECT * FROM runs WHERE run_id=?", (rid,)) or {"run_id": rid}
            row["provenance"] = json.loads(row.get("provenance") or "{}")
            row["summary"] = run_summary(rid)
            row["convergence"] = state.store.query(
                "SELECT iteration, MAX(combined_score) best FROM candidates"
                " WHERE run_id=? AND iteration IS NOT NULL"
                " GROUP BY iteration ORDER BY iteration", (rid,))
            out.append(row)
        return {"runs": out}

    # --------------------------------------------------------- providers
    @app.get("/api/providers")
    def providers() -> Dict[str, Any]:
        snap = state.router.snapshot()
        snap["last_doctor_reports"] = state.last_doctor_reports
        return snap

    @app.get("/api/broker")
    async def broker_status() -> Dict[str, Any]:
        """
        The live state of the OE-MAX broker: the router that actually serves.

        This exists because the Control Center was showing the control plane's
        own `ModelRouter` while every real routing decision was being made in a
        different process, on :8787. An operator could watch a route reported
        healthy here while the broker had its circuit open, or watch a route
        the broker had parked for an exhausted free allowance and see nothing
        at all. Two routers, one of them serving, and the UI showed the other.

        When the broker is not running this reports that plainly. It must not
        synthesise a route table: an operator reading invented health would
        make worse decisions than one reading "not reachable".
        """
        base = os.environ.get("OE_MAX_BASE", "http://127.0.0.1:8787")
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{base}/v1/oe-max/status")
                r.raise_for_status()
                payload = r.json()
        except Exception as exc:
            return {
                "reachable": False,
                "base": base,
                "detail": f"{type(exc).__name__}: {exc}"[:200],
                "router": None,
                "registry": None,
            }
        payload["reachable"] = True
        payload["base"] = base
        return payload

    @app.post("/api/providers/doctor")
    async def run_doctor(probe_tools: bool = True) -> Dict[str, Any]:
        profiles = list(state.router.profiles.values())
        reports = await state.doctor.check_all(profiles, probe_tools=probe_tools)
        apply_reports(profiles, reports)
        state.last_doctor_reports = [r.to_dict() for r in reports]
        for r in reports:
            state.store.execute(
                "INSERT INTO provider_health(provider, model, checked_at, available,"
                " latency_ms, http_status, free_status, detail) VALUES (?,?,?,?,?,?,?,?)",
                (r.provider, r.model, r.checked_at, int(r.available), r.latency_ms,
                 next((p.http_status for p in r.probes if p.http_status), None),
                 r.free_status.value, json.dumps(r.to_dict())),
            )
        return {"reports": state.last_doctor_reports, "routes": state.router.route_table()}

    @app.post("/api/providers/force")
    def force_route(req: ForceRouteRequest) -> Dict[str, Any]:
        try:
            state.router.force(Role(req.role), req.profile_id)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"unknown role {req.role}")
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown profile {req.profile_id}")
        return {"role": req.role, "chain": state.router.role_chains[Role(req.role)]}

    @app.post("/api/providers/{profile_id}/reset-circuit")
    def reset_circuit(profile_id: str) -> Dict[str, Any]:
        state.router.reset_circuit(profile_id)
        return {"profile_id": profile_id, "reset": True}

    # ------------------------------------------------------------ stream
    @app.get("/api/stream")
    async def stream(request: Request, run_id: Optional[str] = None) -> StreamingResponse:
        """
        SSE feed of live events.

        Bounded per-subscriber queue: a slow browser tab drops events and is told
        how many it missed, rather than growing memory in the server without
        limit. The dropped count is reported so the client can refetch.
        """
        q: "queue.Queue[Optional[List[Dict[str, Any]]]]" = queue.Queue(maxsize=256)
        dropped = {"n": 0}

        def on_events(events: List[Event]) -> None:
            batch = [
                e.to_dict() for e in events
                if run_id is None or e.run_id == run_id
            ]
            if not batch:
                return
            try:
                q.put_nowait(batch)
            except queue.Full:
                dropped["n"] += len(batch)

        state.collector.subscribe(on_events)

        async def gen():
            try:
                yield f": connected run={run_id or 'all'}\n\n"
                last_ping = time.time()
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        batch = await asyncio.get_event_loop().run_in_executor(
                            None, lambda: q.get(timeout=1.0))
                    except queue.Empty:
                        batch = None
                    if batch:
                        yield f"event: events\ndata: {json.dumps(batch, default=str)}\n\n"
                    if dropped["n"]:
                        yield (f"event: dropped\ndata: "
                               f'{{"count": {dropped["n"]}}}\n\n')
                        dropped["n"] = 0
                    if time.time() - last_ping > 15:
                        # Keep proxies from closing an idle connection.
                        yield ": ping\n\n"
                        last_ping = time.time()
            finally:
                state.collector.unsubscribe(on_events)

        return StreamingResponse(
            gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                     "Connection": "keep-alive"},
        )

    # ------------------------------------------- classic visualizer bridge
    @app.get("/api/classic")
    def classic_info() -> Dict[str, Any]:
        """
        Where the original OpenEvolve visualizer is, and how to launch it.

        The classic UI stays exactly as upstream ships it — a Flask app on its
        own port reading checkpoints — rather than being reimplemented. Section
        1 requires it preserved and reachable, not replaced.
        """
        script = os.path.join(_ROOT, "scripts", "visualizer.py")
        port = int(os.environ.get("EVOLUTION_CLASSIC_PORT", "8080"))
        runs = []
        for r in state.runner.runs.values():
            ckpts = state.runner.list_checkpoints(r.run_id)
            if ckpts:
                runs.append({"run_id": r.run_id, "name": r.spec.name,
                             "checkpoint_root": os.path.join(r.output_dir, "checkpoints"),
                             "checkpoints": len(ckpts)})
        return {
            "available": os.path.exists(script),
            "script": script,
            "url": f"http://127.0.0.1:{port}",
            "port": port,
            "launch_command": f"{os.path.basename(os.environ.get('VIRTUAL_ENV', '.venv'))}"
                              f"/bin/python scripts/visualizer.py --path <checkpoint_dir>",
            "runs_with_checkpoints": runs,
            "note": "The classic visualizer is upstream's own Flask app, unmodified.",
        }

    # ---------------------------------------------------------- web UI
    web_dist = os.path.join(_ROOT, "web", "dist")
    if os.path.isdir(web_dist):
        app.mount("/assets", StaticFiles(directory=os.path.join(web_dist, "assets")),
                  name="assets")

        @app.get("/", include_in_schema=False)
        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str = "") -> Any:
            if full_path.startswith("api/"):
                raise HTTPException(status_code=404, detail="not found")
            index = os.path.join(web_dist, "index.html")
            if os.path.exists(index):
                return FileResponse(index)
            raise HTTPException(status_code=404, detail="UI not built")
    else:
        @app.get("/", include_in_schema=False)
        def no_ui() -> HTMLResponse:
            return HTMLResponse(
                "<h1>Evolution Control Plane</h1>"
                "<p>API is running. The web UI is not built yet.</p>"
                "<p>Build it with <code>cd web && npm install && npm run build</code>, "
                "or run <code>./dev.sh</code> for the dev server.</p>"
                "<p><a href='/docs'>API documentation</a></p>",
                status_code=200,
            )

    return app


def _count(state: AppState, table: str, run_id: str) -> int:
    row = state.store.query_one(f"SELECT COUNT(*) c FROM {table} WHERE run_id=?", (run_id,))
    return row["c"] if row else 0


def _opencode_isolation_status(state: AppState) -> Dict[str, Any]:
    from ..sandbox.opencode import OpenCodeIsolation

    return OpenCodeIsolation(state.workspace).status()
