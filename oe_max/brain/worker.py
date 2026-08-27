"""
Worker — stdio JSONL host for the evolution engine.

Started by the TypeScript OpenCode plugin (or manually for debugging).

Protocol: JSONL over stdin/stdout (UTF-8, one JSON object per line)

Inbound (stdin) — RpcRequest:
  {"type":"rpc_request","id":"...","method":"evolve/start","params":{...}}
  {"type":"brain_response","id":"...","response":{...}}
  {"type":"cancel","id":"..."}

Outbound (stdout):
  {"type":"rpc_response","id":"...","result":{...},"error":null}
  {"type":"event","event":"generation","data":{...}}
  {"type":"brain_request","id":"...","request":{...}}

The worker owns:
  - evolution lifecycle
  - archives, gates, evaluation, checkpoint/resume
  - budgets, lineage, candidate isolation

It does NOT own:
  - provider, model, credentials (host does)

For now this is a scaffolding that surfaces the evolution engine via RPC
without reimplementing the entire controller. It delegates to the existing
openevolve controller where possible, but via the BrainPort boundary.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from .capabilities import BrainCapabilities
from .port import BrainPort, NullBrainPort
from .stdio_port import StdioBrainPort
from .types import BrainRequest, BrainResponse, PolicyMode

# Worker version — for reproducibility metadata
WORKER_VERSION = "0.1.0-opencode-brainport"


@dataclass
class WorkerState:
    brain: BrainPort = field(default_factory=NullBrainPort)
    capabilities: BrainCapabilities = field(default_factory=BrainCapabilities.minimal)
    runs: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    evolution_tasks: Dict[str, asyncio.Task] = field(default_factory=dict)
    evolution_stats: Dict[str, Any] = field(default_factory=dict)


state = WorkerState()
_writer_ref: Optional[asyncio.StreamWriter] = None
_bg_tasks: set[asyncio.Task] = set()


async def _emit_event(event: str, data: Dict[str, Any]) -> None:
    if _writer_ref is None:
        return
    try:
        line = json.dumps({"type": "event", "event": event, "data": data}, ensure_ascii=False)
        _writer_ref.write((line + "\n").encode("utf-8"))
        await _writer_ref.drain()
    except Exception:
        pass


async def _handle_rpc(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch evolve/* methods — now wired to real evolution loop."""
    if method == "evolve/start":
        run_id = params.get("run_id") or str(uuid.uuid4())
        if run_id in state.evolution_tasks and not state.evolution_tasks[run_id].done():
            return {"run_id": run_id, "status": "already_running", "worker_version": WORKER_VERSION}
        # Record experiment metadata (reproducibility)
        state.runs[run_id] = {
            "run_id": run_id,
            "goal": params.get("goal") or params.get("objective") or "",
            "config": params.get("config") or {},
            "brain_mode": params.get("brain", {}).get("mode", "inherit"),
            "created_at": time.time(),
            "status": "running",
            "generation": 0,
            "worker_version": WORKER_VERSION,
            "host_model_meta": params.get("host_model_meta") or {},
            "base_sha": params.get("base_sha"),
            "iterations": params.get("iterations") or params.get("config", {}).get("iterations", 20),
            "candidates": [],
            "best_score": None,
        }
        host_caps = params.get("capabilities")
        if isinstance(host_caps, dict) and isinstance(state.brain, StdioBrainPort):
            try:
                caps = BrainCapabilities.from_dict(host_caps)
                state.brain.set_capabilities(caps)
                state.capabilities = caps
            except Exception:
                pass

        # Spawn background evolution task
        from .evolution import EvolutionConfig, run_evolution
        from pathlib import Path as _Path

        iterations = int(params.get("iterations") or 20)
        # Build EvolutionConfig from params
        evo_cfg = EvolutionConfig(
            iterations=iterations,
            seed=int(params.get("seed", 42)),
            initial_code=params.get("initial_code") or "def solve(x):\n    return x\n",
            checkpoint_dir=_Path(params.get("checkpoint_dir")) if params.get("checkpoint_dir") else None,
            repo_root=_Path(params.get("repo_root")) if params.get("repo_root") else None,
        )
        # Optional base_sha for cache identity (reserved for future use)

        async def _run():
            # Progress callback that also updates state.runs for status/inspect
            def on_event(ev: str, data: Dict[str, Any]):
                # Update run status
                run = state.runs.get(run_id)
                if run is not None:
                    if ev == "generation":
                        run["generation"] = data.get("generation", run.get("generation"))
                    elif ev == "generation_done":
                        run["generation"] = data.get("generation", run.get("generation"))
                        run["candidates"].append({
                            "candidate": data.get("candidate"),
                            "parent": data.get("parent"),
                            "operator": data.get("operator"),
                            "gate": data.get("gate"),
                            "score": data.get("score"),
                            "delta": data.get("delta"),
                        })
                        if data.get("score") is not None:
                            if run["best_score"] is None or data["score"] > run["best_score"]:
                                run["best_score"] = data["score"]
                    elif ev == "improvement":
                        run["best_score"] = data.get("score", run.get("best_score"))
                # Emit to plugin — hold strong ref to prevent GC
                task = asyncio.create_task(_emit_event(ev, {"run_id": run_id, **data}))
                _bg_tasks.add(task)
                task.add_done_callback(_bg_tasks.discard)

            try:
                stats = await run_evolution(brain=state.brain, config=evo_cfg, on_event=on_event)
                state.evolution_stats[run_id] = stats.__dict__
                run = state.runs.get(run_id)
                if run:
                    run["status"] = "completed"
                    run["generation"] = stats.generation
                    run["best_score"] = stats.best_score if stats.best_score != float("-inf") else run.get("best_score")
                await _emit_event("evolve.completed", {"run_id": run_id, "stats": stats.__dict__})
            except asyncio.CancelledError:
                run = state.runs.get(run_id)
                if run:
                    run["status"] = "cancelled"
                await _emit_event("evolve.cancelled", {"run_id": run_id})
                raise
            except Exception as e:
                run = state.runs.get(run_id)
                if run:
                    run["status"] = "failed"
                    run["error"] = str(e)
                await _emit_event("evolve.failed", {"run_id": run_id, "error": str(e), "trace": traceback.format_exc()})

        task = asyncio.create_task(_run())
        state.evolution_tasks[run_id] = task
        return {"run_id": run_id, "status": "started", "worker_version": WORKER_VERSION, "iterations": iterations}

    if method == "evolve/status":
        run_id = params.get("run_id")
        if run_id and run_id in state.runs:
            run = dict(state.runs[run_id])
            # Attach live stats if available
            if run_id in state.evolution_stats:
                run["stats"] = state.evolution_stats[run_id]
            # Check task state
            task = state.evolution_tasks.get(run_id)
            if task is not None:
                run["task_done"] = task.done()
                run["task_cancelled"] = task.cancelled()
            return {"run": run, "capabilities": state.capabilities.to_dict()}
        # All runs
        out = []
        for rid, run in state.runs.items():
            r = dict(run)
            if rid in state.evolution_stats:
                r["stats"] = state.evolution_stats[rid]
            out.append(r)
        return {"runs": out, "worker_version": WORKER_VERSION}

    if method == "evolve/stop":
        run_id = params.get("run_id")
        if run_id:
            task = state.evolution_tasks.get(run_id)
            if task and not task.done():
                task.cancel()
            if run_id in state.runs:
                state.runs[run_id]["status"] = "stopped"
            return {"ok": True, "run_id": run_id, "cancelled": bool(task and task.cancelled())}
        # Stop all
        for rid, task in list(state.evolution_tasks.items()):
            if not task.done():
                task.cancel()
        for rid in state.runs:
            state.runs[rid]["status"] = "stopped"
        return {"ok": True, "stopped_all": True}

    if method == "evolve/pause":
        run_id = params.get("run_id")
        if run_id in state.runs:
            state.runs[run_id]["status"] = "paused"
            # Note: true pause would suspend the task; for now we mark status
            # The evolution loop checks budgets.exhausted but not pause; this is a placeholder
        return {"ok": True, "run_id": run_id, "status": "paused"}

    if method == "evolve/resume":
        run_id = params.get("run_id")
        if run_id in state.runs:
            state.runs[run_id]["status"] = "running"
        return {"ok": True, "run_id": run_id, "status": "running"}

    if method == "evolve/inspect":
        run_id = params.get("run_id")
        run = state.runs.get(run_id) if run_id else None
        stats = state.evolution_stats.get(run_id) if run_id else None
        # Include recent candidates and budgets
        return {
            "run": run,
            "stats": stats,
            "capabilities": state.capabilities.to_dict(),
            "worker_version": WORKER_VERSION,
            "brain": state.brain.__class__.__name__,
        }

    if method == "evolve/candidates":
        run_id = params.get("run_id")
        run = state.runs.get(run_id) if run_id else None
        cands = (run or {}).get("candidates", []) if run else []
        limit = int(params.get("limit", 50))
        return {"run_id": run_id, "candidates": cands[:limit], "total": len(cands)}

    if method == "evolve/apply":
        run_id = params.get("run_id")
        candidate_id = params.get("candidate_id")
        dry_run = bool(params.get("dry_run", False))
        run = state.runs.get(run_id) if run_id else None
        if not run:
            return {"error": f"run {run_id} not found"}
        # Find candidate
        cand = next((c for c in run.get("candidates", []) if c.get("candidate") == candidate_id), None)
        if not cand and not dry_run:
            return {"error": f"candidate {candidate_id} not found in run {run_id}"}
        if dry_run:
            return {"run_id": run_id, "candidate_id": candidate_id, "dry_run": True, "would_apply": cand is not None}
        # Explicit promotion: for now we just record it; real promotion would copy worktree
        if run:
            run["applied"] = candidate_id
            run["applied_at"] = time.time()
        await _emit_event("evolve.applied", {"run_id": run_id, "candidate_id": candidate_id})
        return {"run_id": run_id, "candidate_id": candidate_id, "status": "applied", "explicit": True}

    if method == "brain/health":
        caps = await state.brain.capabilities()
        return {"healthy": await state.brain.health_check(), "capabilities": caps.to_dict()}

    if method == "brain/generate":
        # Direct brain test — lets the plugin verify the loop without starting evolution
        req = BrainRequest.from_dict(params.get("request") or {})
        resp = await state.brain.generate(req)
        return {"response": resp.to_dict()}

    if method == "brain/update":
        caps = params.get("capabilities")
        model_meta = params.get("model") or params.get("model_meta")
        if isinstance(caps, dict) and isinstance(state.brain, StdioBrainPort):
            try:
                state.capabilities = BrainCapabilities.from_dict(caps)
                state.brain.set_capabilities(state.capabilities)
            except Exception:
                pass
        if isinstance(model_meta, dict):
            # Record as metadata only — no routing change
            state.runs.setdefault("_meta", {})["last_model_switch"] = {"at": time.time(), "model": model_meta}
        return {"ok": True, "capabilities": state.capabilities.to_dict()}

    return {"error": f"unknown method: {method}"}


class _StdoutWriter:
    """Minimal writer for Windows compat — mirrors StreamWriter.write/drain."""
    def write(self, data: bytes) -> None:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    async def drain(self) -> None:
        pass

_stdout_writer = _StdoutWriter()

async def _write_line(writer: Any, obj: Dict[str, Any]) -> None:
    line = json.dumps(obj, ensure_ascii=False)
    # Support both StreamWriter and _StdoutWriter
    try:
        writer.write((line + "\n").encode("utf-8"))
        await writer.drain()
    except Exception:
        # Fallback to stdout
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


async def _run_stdio_loop(
    reader: asyncio.StreamReader, writer: Any
) -> None:
    global _writer_ref
    _writer_ref = writer
    # Attach stdio port if that's the configured brain
    if isinstance(state.brain, StdioBrainPort):
        try:
            state.brain.attach_streams(reader, writer)  # type: ignore
        except Exception:
            pass

    while True:
        try:
            raw = await reader.readline()
        except asyncio.CancelledError:
            break
        if not raw:
            break  # EOF
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            await _write_line(writer, {"type": "error", "error": f"invalid json: {exc}"})
            continue

        mtype = msg.get("type", "rpc_request")
        if mtype == "brain_response":
            # Fulfill a pending brain_request
            if isinstance(state.brain, StdioBrainPort):
                state.brain.handle_brain_response(msg)
            else:
                # No stdio brain — drop
                pass
            continue

        if mtype in ("rpc_request", "request"):
            rid = msg.get("id") or str(uuid.uuid4())
            method = msg.get("method") or msg.get("op") or ""
            params = msg.get("params") or msg.get("args") or {}
            try:
                result = await _handle_rpc(method, params)
                await _write_line(writer, {"type": "rpc_response", "id": rid, "result": result, "error": None})
                # Also emit a synthetic event for observability
                if method == "evolve/start":
                    await _write_line(writer, {"type": "event", "event": "evolve.started", "data": result})
            except Exception as exc:
                await _write_line(
                    writer,
                    {
                        "type": "rpc_response",
                        "id": rid,
                        "result": None,
                        "error": {"message": str(exc), "trace": traceback.format_exc()},
                    },
                )
            continue

        if mtype == "cancel":
            rid = msg.get("id")
            # If id is a run_id, cancel that evolution
            run_id = msg.get("run_id") or rid
            if run_id and run_id in state.evolution_tasks:
                task = state.evolution_tasks[run_id]
                if not task.done():
                    task.cancel()
                    if run_id in state.runs:
                        state.runs[run_id]["status"] = "cancelled"
            await _write_line(writer, {"type": "rpc_response", "id": rid, "result": {"ok": True, "run_id": run_id}, "error": None})
            continue
        if mtype == "brain_update" or (mtype == "rpc_request" and msg.get("method") == "brain/update"):
            # Handle model/capability switch without restart — no source change required
            caps = msg.get("capabilities") or msg.get("params", {}).get("capabilities")
            if isinstance(caps, dict) and isinstance(state.brain, StdioBrainPort):
                try:
                    state.capabilities = BrainCapabilities.from_dict(caps)
                    state.brain.set_capabilities(state.capabilities)
                except Exception:
                    pass
            rid = msg.get("id")
            if rid:
                await _write_line(writer, {"type": "rpc_response", "id": rid, "result": {"ok": True, "capabilities": state.capabilities.to_dict()}, "error": None})
            continue

        # Unknown
        await _write_line(writer, {"type": "error", "error": f"unknown message type: {mtype}"})


async def main_async(argv: Optional[list] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="OpenEvo BrainPort worker (stdio JSONL)")
    parser.add_argument("--brain", choices=["null", "legacy", "stdio"], default="stdio", help="brain backend")
    parser.add_argument("--stdio", action="store_true", help="force stdio mode (default)")
    args = parser.parse_args(argv)

    # Configure brain
    if args.brain == "legacy":
        from .legacy_adapter import LegacyBrainPort

        state.brain = LegacyBrainPort()
        state.capabilities = await state.brain.capabilities()
    elif args.brain == "null":
        state.brain = NullBrainPort()
        state.capabilities = await state.brain.capabilities()
    else:
        # stdio — will be attached once the loop starts; use minimal caps until host sends real ones
        state.brain = StdioBrainPort()
        state.capabilities = BrainCapabilities.minimal()

    # Emit hello — use Windows-compatible stdout
    writer_stream: Any = _stdout_writer
    hello = {
        "type": "hello",
        "worker_version": WORKER_VERSION,
        "brain": args.brain,
        "capabilities": state.capabilities.to_dict(),
        "pid": __import__("os").getpid(),
    }
    await _write_line(writer_stream, hello)

    # Windows-compatible stdin: feed StreamReader via thread reading sys.stdin.buffer
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()

    def _feed_stdin():
        try:
            while True:
                line = sys.stdin.buffer.readline()
                if not line:
                    # EOF
                    loop.call_soon_threadsafe(reader.feed_eof)
                    break
                loop.call_soon_threadsafe(reader.feed_data, line)
        except Exception:
            try:
                loop.call_soon_threadsafe(reader.feed_eof)
            except Exception:
                pass

    import threading
    t = threading.Thread(target=_feed_stdin, daemon=True)
    t.start()

    await _run_stdio_loop(reader, writer_stream)

    # Cleanup
    try:
        await state.brain.close()
    except Exception:
        pass
    return 0


def main(argv: Optional[list] = None) -> int:
    return asyncio.run(main_async(argv))


if __name__ == "__main__":
    raise SystemExit(main())
