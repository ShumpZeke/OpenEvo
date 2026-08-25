"""
Run manager — real start/stop/checkpoint/resume over engine subprocesses.

Every control here maps to an actual operation on a real process. Where upstream
cannot support a control, `capabilities()` reports it unsupported with a reason
so the UI can disable the button and say why (SOURCE_OF_TRUTH section 15) rather
than showing a control that does nothing.

Pause is the honest example: OpenEvolve has no resumable in-place pause. SIGSTOP
would freeze the process but leaves provider sockets and worker pools in an
undefined state, so we report pause as unsupported and point the operator at
stop→checkpoint→resume, which genuinely works.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..telemetry.bus import emit
from ..telemetry.events import Component, Event, EventType, Status, new_id
from ..telemetry import instrument as _instr
from .entrypoint import CHECKPOINT_REQUEST_FILE

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@dataclass
class RunSpec:
    initial_program: str
    evaluator: str
    config_path: Optional[str] = None
    iterations: Optional[int] = None
    target_score: Optional[float] = None
    checkpoint: Optional[str] = None
    name: str = "experiment"
    env: Dict[str, str] = field(default_factory=dict)

    def validate(self) -> List[str]:
        problems = []
        if not os.path.exists(self.initial_program):
            problems.append(f"initial program not found: {self.initial_program}")
        if not os.path.exists(self.evaluator):
            problems.append(f"evaluator not found: {self.evaluator}")
        if self.config_path and not os.path.exists(self.config_path):
            problems.append(f"config not found: {self.config_path}")
        if self.checkpoint and not os.path.exists(self.checkpoint):
            problems.append(f"checkpoint not found: {self.checkpoint}")
        return problems


@dataclass
class ManagedRun:
    run_id: str
    experiment_id: str
    spec: RunSpec
    output_dir: str
    control_dir: str
    event_log: str
    process: Optional[subprocess.Popen] = None
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    status: str = "created"
    exit_code: Optional[int] = None
    stdout_path: str = ""
    stderr_path: str = ""

    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "experiment_id": self.experiment_id,
            "name": self.spec.name,
            "status": self.status,
            "pid": self.process.pid if self.process else None,
            "alive": self.is_alive(),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "exit_code": self.exit_code,
            "output_dir": self.output_dir,
            "event_log": self.event_log,
            "iterations": self.spec.iterations,
            "checkpoint": self.spec.checkpoint,
        }


class RunManager:
    """Owns the lifecycle of engine subprocesses."""

    # (supported, reason)
    CAPABILITIES: Dict[str, Any] = {
        "start": (True, ""),
        "graceful_stop": (True, "SIGTERM triggers the engine's cooperative shutdown"),
        "force_stop": (True, "SIGKILL; no final checkpoint is written"),
        "checkpoint_now": (
            True,
            "Requested via control file; taken at the next safe iteration boundary",
        ),
        "resume_checkpoint": (True, "Re-launches with --checkpoint"),
        "clone_experiment": (True, "Re-launches with the same config revision"),
        "fork_from_candidate": (
            False,
            "Upstream has no API to seed a run from an arbitrary candidate; "
            "resume from the checkpoint containing it instead.",
        ),
        "pause_resume_in_place": (
            False,
            "OpenEvolve has no resumable in-place pause. SIGSTOP would leave "
            "provider connections and the worker pool in an undefined state. "
            "Use graceful stop → resume from checkpoint.",
        ),
        "retry_failed_evaluation": (
            False,
            "Evaluation retries are driven by the engine's own retry policy; "
            "the control plane cannot re-inject a single candidate mid-run.",
        ),
    }

    def __init__(self, workspace: str, collector_port: Optional[int] = None) -> None:
        self.workspace = os.path.abspath(workspace)
        self.collector_port = collector_port
        self.runs: Dict[str, ManagedRun] = {}
        self._lock = threading.Lock()
        os.makedirs(self.workspace, exist_ok=True)

    # -- capability reporting -----------------------------------------

    def capabilities(self) -> Dict[str, Dict[str, Any]]:
        return {
            k: {"supported": v[0], "reason": v[1]} for k, v in self.CAPABILITIES.items()
        }

    # -- lifecycle -----------------------------------------------------

    def start(self, spec: RunSpec, experiment_id: Optional[str] = None) -> ManagedRun:
        problems = spec.validate()
        if problems:
            raise ValueError("; ".join(problems))

        run_id = new_id("run_")
        experiment_id = experiment_id or new_id("exp_")
        run_dir = os.path.join(self.workspace, run_id)
        output_dir = os.path.join(run_dir, "output")
        control_dir = os.path.join(run_dir, "control")
        os.makedirs(output_dir, exist_ok=True)
        os.makedirs(control_dir, exist_ok=True)
        event_log = os.path.join(run_dir, "events.ndjson")

        run = ManagedRun(
            run_id=run_id, experiment_id=experiment_id, spec=spec,
            output_dir=output_dir, control_dir=control_dir, event_log=event_log,
            stdout_path=os.path.join(run_dir, "stdout.log"),
            stderr_path=os.path.join(run_dir, "stderr.log"),
        )

        cmd = [sys.executable, "-m", "control_plane.runner.entrypoint",
               spec.initial_program, spec.evaluator, "--output", output_dir]
        if spec.config_path:
            cmd += ["--config", spec.config_path]
        if spec.iterations is not None:
            cmd += ["--iterations", str(spec.iterations)]
        if spec.target_score is not None:
            cmd += ["--target-score", str(spec.target_score)]
        if spec.checkpoint:
            cmd += ["--checkpoint", spec.checkpoint]

        env = os.environ.copy()
        env.update(spec.env)
        env.update({
            _instr.ENV_ENABLED: "1",
            _instr.ENV_RUN_ID: run_id,
            _instr.ENV_EXPERIMENT_ID: experiment_id,
            _instr.ENV_EVENT_LOG: event_log,
            "EVOLUTION_CONTROL_DIR": control_dir,
            "PYTHONPATH": _ROOT + os.pathsep + env.get("PYTHONPATH", ""),
            # Unbuffered so the log tail in the UI is live rather than chunked.
            "PYTHONUNBUFFERED": "1",
        })
        if self.collector_port:
            env[_instr.ENV_COLLECTOR_PORT] = str(self.collector_port)

        # Emit creation from the control plane so the run exists in the UI even
        # if the subprocess dies before it can emit anything itself.
        emit(Event(
            type=EventType.EXPERIMENT_CREATED, component=Component.CONTROL_PLANE,
            run_id=run_id, experiment_id=experiment_id,
            summary=f"experiment '{spec.name}' created",
            metadata={
                "name": spec.name, "config_path": spec.config_path,
                "initial_program": spec.initial_program,
                "evaluator_path": spec.evaluator, "iterations": spec.iterations,
                "output_dir": output_dir, "checkpoint": spec.checkpoint,
                "command": cmd,
            },
        ))

        stdout_fh = open(run.stdout_path, "wb")
        stderr_fh = open(run.stderr_path, "wb")
        popen_kwargs: Dict[str, Any] = {
            "cwd": _ROOT, "env": env, "stdout": stdout_fh, "stderr": stderr_fh,
        }
        if os.name == "posix":
            # Own process group so force-stop reaps worker processes too;
            # otherwise ProcessPoolExecutor children survive the kill.
            popen_kwargs["start_new_session"] = True
        run.process = subprocess.Popen(cmd, **popen_kwargs)
        run.status = "running"

        with self._lock:
            self.runs[run_id] = run

        threading.Thread(target=self._reap, args=(run, stdout_fh, stderr_fh),
                         name=f"reap-{run_id}", daemon=True).start()
        return run

    def _reap(self, run: ManagedRun, *handles) -> None:
        assert run.process is not None
        code = run.process.wait()
        run.exit_code = code
        run.ended_at = time.time()
        for fh in handles:
            try:
                fh.close()
            except Exception:
                pass
        if run.status == "stopping":
            run.status = "stopped"
        elif code == 0:
            run.status = "completed"
        elif code in (130, -signal.SIGTERM if hasattr(signal, "SIGTERM") else -15):
            run.status = "stopped"
        else:
            run.status = "failed"

        tail = ""
        if run.status == "failed":
            try:
                with open(run.stderr_path, "r", encoding="utf-8", errors="replace") as fh:
                    tail = fh.read()[-4000:]
            except OSError:
                pass
        emit(Event(
            type={
                "completed": EventType.EXPERIMENT_COMPLETED,
                "stopped": EventType.EXPERIMENT_STOPPED,
                "failed": EventType.EXPERIMENT_FAILED,
            }.get(run.status, EventType.EXPERIMENT_STOPPED),
            component=Component.CONTROL_PLANE,
            run_id=run.run_id, experiment_id=run.experiment_id,
            status=Status.OK if run.status == "completed" else Status.FAILED,
            summary=f"run {run.status} (exit code {code})",
            metrics={"exit_code": float(code)},
            error={"type": "ProcessExit", "message": tail} if tail else None,
            metadata={"stderr_tail": tail} if tail else {},
        ))

    def stop(self, run_id: str, force: bool = False, timeout: float = 30.0) -> Dict[str, Any]:
        run = self.runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        if not run.is_alive():
            return {"run_id": run_id, "status": run.status, "note": "already stopped"}

        assert run.process is not None
        run.status = "stopping"
        self._audit(run, "force_stop" if force else "graceful_stop")

        if force:
            self._kill_group(run, signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM)
            run.process.wait(timeout=10)
            return {"run_id": run_id, "status": "stopped", "forced": True}

        self._kill_group(run, signal.SIGTERM)
        try:
            run.process.wait(timeout=timeout)
            return {"run_id": run_id, "status": "stopped", "forced": False}
        except subprocess.TimeoutExpired:
            # Graceful path exhausted; escalate rather than leaving a zombie run.
            self._kill_group(run, signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM)
            run.process.wait(timeout=10)
            return {
                "run_id": run_id, "status": "stopped", "forced": True,
                "note": f"graceful stop timed out after {timeout}s; escalated to force",
            }

    @staticmethod
    def _kill_group(run: ManagedRun, sig: int) -> None:
        assert run.process is not None
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(run.process.pid), sig)
            else:
                run.process.send_signal(sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                run.process.send_signal(sig)
            except Exception:
                pass

    def checkpoint_now(self, run_id: str) -> Dict[str, Any]:
        run = self.runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        if not run.is_alive():
            raise RuntimeError("run is not active")
        path = os.path.join(run.control_dir, CHECKPOINT_REQUEST_FILE)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(str(time.time()))
        self._audit(run, "checkpoint_now")
        return {
            "run_id": run_id, "requested": True,
            "note": "checkpoint will be written at the next iteration boundary",
        }

    def resume(self, run_id: str, checkpoint: Optional[str] = None,
               iterations: Optional[int] = None) -> ManagedRun:
        """Start a NEW run seeded from a checkpoint of an existing one."""
        old = self.runs.get(run_id)
        if old is None:
            raise KeyError(run_id)
        ckpt = checkpoint or self.latest_checkpoint(run_id)
        if not ckpt:
            raise RuntimeError(f"no checkpoint found for {run_id}")
        spec = RunSpec(
            initial_program=old.spec.initial_program,
            evaluator=old.spec.evaluator,
            config_path=old.spec.config_path,
            iterations=iterations if iterations is not None else old.spec.iterations,
            target_score=old.spec.target_score,
            checkpoint=ckpt,
            name=f"{old.spec.name} (resumed)",
            env=dict(old.spec.env),
        )
        return self.start(spec, experiment_id=old.experiment_id)

    def clone(self, run_id: str, name: Optional[str] = None,
              iterations: Optional[int] = None) -> ManagedRun:
        old = self.runs.get(run_id)
        if old is None:
            raise KeyError(run_id)
        spec = RunSpec(
            initial_program=old.spec.initial_program,
            evaluator=old.spec.evaluator,
            config_path=old.spec.config_path,
            iterations=iterations if iterations is not None else old.spec.iterations,
            target_score=old.spec.target_score,
            name=name or f"{old.spec.name} (clone)",
            env=dict(old.spec.env),
        )
        return self.start(spec)

    def latest_checkpoint(self, run_id: str) -> Optional[str]:
        run = self.runs.get(run_id)
        if run is None:
            return None
        ckpt_root = os.path.join(run.output_dir, "checkpoints")
        if not os.path.isdir(ckpt_root):
            return None
        entries = []
        for name in os.listdir(ckpt_root):
            if not name.startswith("checkpoint_"):
                continue
            try:
                entries.append((int(name.split("_", 1)[1]), os.path.join(ckpt_root, name)))
            except ValueError:
                continue
        return max(entries)[1] if entries else None

    def list_checkpoints(self, run_id: str) -> List[Dict[str, Any]]:
        run = self.runs.get(run_id)
        if run is None:
            return []
        root = os.path.join(run.output_dir, "checkpoints")
        if not os.path.isdir(root):
            return []
        out = []
        for name in sorted(os.listdir(root)):
            if not name.startswith("checkpoint_"):
                continue
            path = os.path.join(root, name)
            try:
                iteration = int(name.split("_", 1)[1])
            except ValueError:
                continue
            size = sum(
                os.path.getsize(os.path.join(dp, f))
                for dp, _dn, fns in os.walk(path) for f in fns
                if os.path.exists(os.path.join(dp, f))
            )
            meta_path = os.path.join(path, "metadata.json")
            meta = {}
            if os.path.exists(meta_path):
                try:
                    with open(meta_path) as fh:
                        meta = json.load(fh)
                except Exception:
                    pass
            out.append({
                "checkpoint_id": f"{run_id}:{iteration}", "run_id": run_id,
                "iteration": iteration, "path": path, "size_bytes": size,
                "created_at": os.path.getmtime(path),
                "num_programs": len(meta.get("programs", []) or []) or meta.get("num_programs"),
                "best_score": meta.get("best_score"),
            })
        return out

    def delete_checkpoint(self, run_id: str, iteration: int) -> bool:
        run = self.runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        path = os.path.join(run.output_dir, "checkpoints", f"checkpoint_{iteration}")
        # Refuse to delete outside the run's own directory even if the caller
        # supplies a crafted iteration value.
        if not os.path.abspath(path).startswith(os.path.abspath(run.output_dir)):
            raise ValueError("refusing to delete outside the run output directory")
        if not os.path.isdir(path):
            return False
        shutil.rmtree(path)
        self._audit(run, "delete_checkpoint", {"iteration": iteration})
        return True

    def list_runs(self) -> List[Dict[str, Any]]:
        return [r.to_dict() for r in self.runs.values()]

    def get(self, run_id: str) -> Optional[ManagedRun]:
        return self.runs.get(run_id)

    def log_tail(self, run_id: str, stream: str = "stdout", lines: int = 200) -> str:
        run = self.runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        path = run.stdout_path if stream == "stdout" else run.stderr_path
        if not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-lines:])

    def shutdown(self) -> None:
        for run_id, run in list(self.runs.items()):
            if run.is_alive():
                try:
                    self.stop(run_id, force=False, timeout=10)
                except Exception:
                    pass

    def _audit(self, run: ManagedRun, command: str,
               extra: Optional[Dict[str, Any]] = None) -> None:
        emit(Event(
            type=EventType.CONTROL_COMMAND, component=Component.CONTROL_PLANE,
            run_id=run.run_id, experiment_id=run.experiment_id,
            summary=f"control command: {command}",
            metadata={"command": command, **(extra or {})},
        ))
