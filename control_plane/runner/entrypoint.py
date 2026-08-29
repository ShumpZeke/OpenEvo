"""
Evolution run entrypoint.

Executed as a subprocess by the run manager. It configures telemetry, installs
the engine hooks (in this process and in every ProcessPoolExecutor worker), then
hands control to OpenEvolve's ordinary CLI.

The CLI is invoked unmodified. That is the point: the same `openevolve-run.py`
the operator runs by hand is what runs here, so a run started from the Control
Center and a run started from the terminal execute identical engine code.

Usage:
    python -m control_plane.runner.entrypoint --config ... --iterations ... \
        <initial_program> <evaluator>
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import threading
import time
from typing import Optional

# Repo root on sys.path so `control_plane` and `openevolve` both import when
# this is spawned as a bare subprocess.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from control_plane.telemetry import instrument as _instr  # noqa: E402
from control_plane.telemetry.bus import configure_bus, emit, get_bus  # noqa: E402
from control_plane.telemetry.events import (  # noqa: E402
    Component, Event, EventType, Status,
)
from oe_max.console import use_utf8_stdio  # noqa: E402


def _ensure_utf8_logging() -> None:
    """
    Make this process and its workers able to write the log lines they produce.

    Upstream marks a new best with a star and writes score changes with an
    arrow. On Windows the console code page is cp1252, `logging` cannot encode
    them, the handler raises, and the **whole record is discarded** -- measured
    at 0 bytes written against 48 with UTF-8. So a run that found a new best can
    produce a log that never says so.

    Both halves are needed and they cover different processes:

    * `PYTHONIOENCODING` is set in the environment *before* any worker is
      spawned. Under `spawn` -- the default on Windows and macOS -- each worker
      is a fresh interpreter that inherits this environment, and workers log
      too. Reconfiguring streams in this process does nothing for them.
    * `use_utf8_stdio()` fixes *this* interpreter, whose streams already exist
      and will not re-read the variable.

    The run manager sets the variable for runs it launches. This covers the
    other ways in -- `scripts/resume-evolution.*`, and anyone invoking this
    module directly.

    An *empty* value counts as unset. Python ignores `PYTHONIOENCODING=""` and
    falls back to the code page, but `setdefault` would see the key as present
    and leave it -- so a blank value, which a shell or a container can easily
    pass through, would hand every worker the encoding this exists to avoid. A
    non-empty value is left alone: someone who set it deliberately gets what
    they asked for, and the failure being guarded against is absence.
    """
    if not os.environ.get("PYTHONIOENCODING", "").strip():
        os.environ["PYTHONIOENCODING"] = "utf-8"
    use_utf8_stdio()


_ensure_utf8_logging()

CHECKPOINT_REQUEST_FILE = "checkpoint.request"
STOP_REQUEST_FILE = "stop.request"


def _control_dir() -> str:
    return os.environ.get("EVOLUTION_CONTROL_DIR", ".")


def _install_checkpoint_on_demand() -> None:
    """
    Make "Checkpoint now" a real operation rather than a disabled button.

    Upstream checkpoints on a fixed interval and exposes no on-demand trigger.
    Rather than fake the control or bolt on a signal handler that could fire
    mid-mutation, we poll a request file at a point that is already safe: just
    after `ProgramDatabase.add` returns in the *main* process. At that moment
    the database is in the same consistent state the interval checkpoint writes
    from, so `_save_checkpoint` does exactly what it always does.

    A file (not SIGUSR1) because Windows is a first-class target and has no
    SIGUSR1. The cost is one os.path.exists per added candidate.
    """
    try:
        from openevolve.controller import OpenEvolve
        from openevolve.database import ProgramDatabase
    except ImportError:
        return

    state = {"controller": None, "main_pid": os.getpid(), "last": 0.0}

    original_init = OpenEvolve.__init__

    def init_wrapper(self, *a, **kw):
        original_init(self, *a, **kw)
        state["controller"] = self

    OpenEvolve.__init__ = init_wrapper  # type: ignore[method-assign]

    original_add = ProgramDatabase.add

    def add_wrapper(db_self, program, iteration=None, target_island=None, *a, **kw):
        result = original_add(db_self, program, iteration, target_island, *a, **kw)
        # Workers must never checkpoint; only the process owning the database.
        if os.getpid() != state["main_pid"]:
            return result
        req = os.path.join(_control_dir(), CHECKPOINT_REQUEST_FILE)
        try:
            if os.path.exists(req):
                os.remove(req)
                ctl = state["controller"]
                it = iteration if iteration is not None else getattr(
                    db_self, "last_iteration", 0
                )
                if ctl is not None:
                    ctl._save_checkpoint(int(it or 0))
                else:
                    emit(Event(
                        type=EventType.CHECKPOINT_FAILED, component=Component.CONTROLLER,
                        run_id=os.environ.get(_instr.ENV_RUN_ID), status=Status.FAILED,
                        summary="checkpoint requested before controller was ready",
                    ))
        except Exception as exc:
            emit(Event(
                type=EventType.CHECKPOINT_FAILED, component=Component.CONTROLLER,
                run_id=os.environ.get(_instr.ENV_RUN_ID), status=Status.FAILED,
                summary=f"on-demand checkpoint failed: {exc}",
                error={"type": type(exc).__name__, "message": str(exc)},
            ))
        return result

    ProgramDatabase.add = add_wrapper  # type: ignore[method-assign]


def _start_resource_sampler(run_id: str, interval: float = 5.0) -> threading.Thread:
    """
    Sample real process/system resources.

    Uses psutil when available and degrades to /proc on Linux. If neither can
    supply a metric we emit nothing for it — the Resource timeline shows a gap,
    which is truthful, rather than a zero, which is not.
    """

    def sample() -> None:
        proc = None
        psutil = None
        try:
            import psutil as _ps

            psutil = _ps
            proc = psutil.Process()
            proc.cpu_percent(None)  # prime the counter
        except Exception:
            pass

        while True:
            try:
                metrics = {}
                if psutil and proc:
                    metrics["cpu"] = (proc.cpu_percent(None), "percent")
                    mi = proc.memory_info()
                    metrics["ram"] = (mi.rss / (1024 * 1024), "MiB")
                    try:
                        du = psutil.disk_usage(os.getcwd())
                        metrics["disk"] = (du.percent, "percent")
                    except Exception:
                        pass
                else:
                    # /proc fallback: RSS only, which is better than nothing.
                    try:
                        with open(f"/proc/{os.getpid()}/statm") as fh:
                            rss_pages = int(fh.read().split()[1])
                        metrics["ram"] = (
                            rss_pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024),
                            "MiB",
                        )
                    except Exception:
                        pass

                for kind, (value, unit) in metrics.items():
                    emit(Event(
                        type=EventType(f"resource.{kind}"),
                        component=Component.RESOURCE,
                        run_id=run_id,
                        summary=f"{kind}={value:.2f}{unit}",
                        metrics={"value": float(value)},
                        metadata={"unit": unit, "scope": "engine_process"},
                    ))

                bus = get_bus()
                if bus is not None:
                    emit(bus.health_event())
            except Exception:
                pass
            time.sleep(interval)

    t = threading.Thread(target=sample, name="evolution-resources", daemon=True)
    t.start()
    return t


def main() -> int:
    run_id = os.environ.get(_instr.ENV_RUN_ID)
    if not run_id:
        print("EVOLUTION_RUN_ID must be set", file=sys.stderr)
        return 2

    os.environ.setdefault(_instr.ENV_ENABLED, "1")
    configure_bus(
        ndjson_path=os.environ.get(_instr.ENV_EVENT_LOG),
        socket_port=int(os.environ[_instr.ENV_COLLECTOR_PORT])
        if os.environ.get(_instr.ENV_COLLECTOR_PORT) else None,
    )
    from control_plane.telemetry.redaction import default_redactor

    default_redactor().register_env(
        "OPENAI_API_KEY", "NVIDIA_API_KEY", "NIM_API_KEY", "OPENCODE_API_KEY",
        "ZEN_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY",
        "EVOLUTION_PROVIDER_KEY",
    )

    _instr.auto_install_from_env()
    _instr.install_worker_hook()
    _install_checkpoint_on_demand()
    _start_resource_sampler(run_id)

    # Graceful stop: hand SIGTERM to the engine's own handler, which requests a
    # cooperative shutdown and lets it write a final checkpoint.
    def on_term(signum, frame):
        emit(Event(
            type=EventType.EXPERIMENT_STOPPED, component=Component.CONTROLLER,
            run_id=run_id, status=Status.CANCELLED,
            summary=f"stop requested (signal {signum}); shutting down gracefully",
        ))
        raise KeyboardInterrupt()

    if hasattr(signal, "SIGTERM"):
        previous = signal.getsignal(signal.SIGTERM)

        def chain(signum, frame):
            # The engine installs its own SIGTERM handler once running; call it
            # first so cooperative shutdown still happens, then record the event.
            if callable(previous) and previous not in (
                signal.SIG_DFL, signal.SIG_IGN
            ):
                try:
                    previous(signum, frame)
                except Exception:
                    pass
            on_term(signum, frame)

        signal.signal(signal.SIGTERM, chain)

    from openevolve.cli import main as cli_main

    try:
        code = cli_main()
    except KeyboardInterrupt:
        code = 130
    finally:
        bus = get_bus()
        if bus is not None:
            bus.flush(timeout=10.0)
            bus.close()
    return int(code or 0)


if __name__ == "__main__":
    sys.exit(main())
