"""
Evaluate candidates in a sandbox instead of in the evaluator's own process.

Upstream's `Evaluator._direct_evaluate` calls the task's evaluation function on
a thread — so an evolved program, which is code nobody wrote and nobody
reviewed, executes inside the run with no ceilings at all. A candidate with a
runaway loop consumes the evaluator; one that allocates without bound takes the
process with it; one that spawns workers leaves them behind.

What is sandboxed is the **evaluation function**, not the candidate alone. The
evaluator is trusted code, but it is the thing that imports and runs the
untrusted candidate, so the boundary has to sit outside it. That also means the
evaluator's own imports keep working: the script adds its directory to
`sys.path` exactly as upstream does.

Off unless `OE_MAX_SANDBOX_EVAL` is set. This changes how evaluation runs — a
candidate that relied on shared process state will behave differently, and the
wall-clock ceiling can turn a slow pass into a timeout — so it is a deliberate
choice, not a default.
"""

from __future__ import annotations

import functools
import json
import logging
import os
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

ENV_SANDBOX_EVAL = "OE_MAX_SANDBOX_EVAL"
ENV_BACKEND = "OE_MAX_SANDBOX_BACKEND"
ENV_MEMORY_MB = "OE_MAX_SANDBOX_MEMORY_MB"


def enabled() -> bool:
    return os.environ.get(ENV_SANDBOX_EVAL, "").lower() in ("1", "true", "yes", "on")


# The script run inside the sandbox. It reproduces exactly what upstream does —
# load the evaluation module from its file, call one function on the program
# path — and then writes the result somewhere the parent can read it.
#
# Metrics come back through a file rather than stdout because an evaluator that
# prints is entirely normal, and mixing the two makes a diagnostic
# indistinguishable from a measurement.
_SCRIPT = '''
import importlib.util, json, os, sys

_eval_file = {eval_file}
_program = {program}
_fn_name = {fn_name}

sys.path.insert(0, os.path.dirname(os.path.abspath(_eval_file)))

_spec = importlib.util.spec_from_file_location("evaluation_module", _eval_file)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

_fn = getattr(_module, _fn_name, None)
if _fn is None:
    raise SystemExit("evaluation function %r is not defined" % _fn_name)

_result = _fn(_program)

# EvaluationResult carries metrics plus artifacts; a plain dict is metrics
# only. Both shapes are flattened here so the parent has one thing to parse.
_metrics = getattr(_result, "metrics", None)
if _metrics is None and isinstance(_result, dict):
    _metrics = _result
_artifacts = getattr(_result, "artifacts", None) or {{}}


def _plain(value):
    """Anything that will not serialise comes back as its repr, never dropped."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


# The filename is the runner's contract, not an arbitrary choice: it deletes
# the working directory after the run, and this is the one file it reads back
# before doing so.
with open("oe_max_result.json", "w", encoding="utf-8") as _fh:
    json.dump({{"metrics": {{k: _plain(v) for k, v in (_metrics or {{}}).items()}},
               "artifacts": {{k: _plain(v) for k, v in _artifacts.items()}}}}, _fh)
'''


def evaluate_in_sandbox(eval_file: str, program_path: str, fn_name: str,
                        timeout_s: float) -> Tuple[Optional[Dict[str, Any]], Any]:
    """
    Run one evaluation under resource ceilings.

    Returns `(payload, result)`: the payload is what the sandbox wrote, and
    `result` is the ExecutionResult so a caller can tell a timeout from a crash
    and say which in the metrics it synthesises.
    """
    from oe_max.execution import ResourceLimits, SandboxedRunner

    memory_mb = _int_env(ENV_MEMORY_MB, 2048)
    limits = ResourceLimits(
        # The evaluator's own timeout is the contract the task was written
        # against; a tighter one here would turn passing candidates into
        # timeouts for reasons the task never agreed to. CPU is given headroom
        # over wall clock so the wall clock stays the binding limit.
        wall_seconds=float(timeout_s),
        cpu_seconds=int(timeout_s * 2) + 5,
        memory_mb=memory_mb,
    )
    runner = SandboxedRunner(limits,
                             backend=os.environ.get(ENV_BACKEND, "auto"))
    script = _SCRIPT.format(eval_file=json.dumps(os.path.abspath(eval_file)),
                            program=json.dumps(os.path.abspath(program_path)),
                            fn_name=json.dumps(fn_name))
    # The script above embeds host absolute paths, which a container cannot see
    # unless they are mounted. Naming them here keeps the exposure minimal and
    # explicit: the evaluator's directory and the candidate's, read-only, and
    # nothing else. The subprocess backend already shares the filesystem and
    # ignores this.
    result = runner.run_script(
        script, read_only_paths=(os.path.abspath(eval_file),
                                 os.path.abspath(program_path)))
    return _payload(result), result


def _payload(result: Any) -> Optional[Dict[str, Any]]:
    """
    The evaluation's own output, if it produced any.

    `run_script` deletes the working directory, so the value has to come back
    through the runner's own channel — `ExecutionResult.value`, which it reads
    from `oe_max_result.json` before cleaning up.
    """
    value = getattr(result, "value", None)
    return value if isinstance(value, dict) else None


def failure_metrics(result: Any) -> Dict[str, float]:
    """
    What to report when the sandbox stopped the evaluation.

    Zeroed metrics, which is exactly what upstream reports for an evaluation
    that raised — a candidate killed for exhausting memory scored nothing, and
    saying so keeps it comparable to every other failed candidate rather than
    creating a second kind of failure the archive has to understand.
    """
    return {"combined_score": 0.0, "error": 1.0}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, TypeError, ValueError):
        return default


def install(instrumentation: Any) -> bool:
    """
    Replace upstream's in-process evaluation with a sandboxed one.

    Wraps `_direct_evaluate`, which is the frame that calls the task's
    evaluation function on a thread. Returns whether the hook was installed, so
    a caller can report "asked for a sandbox and did not get one" rather than
    assume.
    """
    if not enabled():
        return False
    try:
        from openevolve.evaluator import Evaluator
    except ImportError:
        return False

    original = getattr(Evaluator, "_direct_evaluate", None)
    if original is None or getattr(original, "__evolution_instrumented__", False):
        return False

    @functools.wraps(original)
    async def wrapper(ev_self, program_path, *a, **kw):
        eval_file = getattr(ev_self, "evaluation_file", None)
        timeout = getattr(getattr(ev_self, "config", None), "timeout", 60) or 60
        if not eval_file:
            return await original(ev_self, program_path, *a, **kw)

        payload, result = evaluate_in_sandbox(eval_file, program_path,
                                              "evaluate", float(timeout))
        if result.ok and payload is not None:
            metrics = payload.get("metrics") or {}
            return {k: float(v) for k, v in metrics.items()
                    if isinstance(v, (int, float, bool))}

        # A sandbox that could not run at all is a configuration problem, not a
        # verdict on the candidate — fall back rather than scoring every
        # program zero and quietly ruining the run.
        if result.status == "backend_unavailable":
            logger.warning("sandboxed evaluation unavailable (%s); "
                           "falling back to in-process", result.reason)
            return await original(ev_self, program_path, *a, **kw)

        logger.info("sandboxed evaluation failed (%s): %s",
                    result.status, result.reason or "")
        return failure_metrics(result)

    wrapper.__evolution_instrumented__ = True  # type: ignore[attr-defined]
    Evaluator._direct_evaluate = wrapper       # type: ignore[method-assign]
    logger.info("OE-MAX sandboxed evaluation enabled (%s)", ENV_SANDBOX_EVAL)
    return True
