"""
Start a run from a population instead of from one program.

`oe_max/search/seed_forge` produces variants of the seed without a model
request. This is the part that puts them in the database — otherwise it is one
more thing that is built and not in the loop, which is the failure mode this
project keeps hitting.

Where the variants go matters as much as that they exist. They are spread
across islands round-robin, because the problem being solved is that every
island starts in the same basin: upstream seeds island 0 and lets migration
spread it, so for the first several generations the island structure is
separating populations that are identical. A forged population gives migration
something to exchange from the first generation.

Each variant is evaluated before it is added. An unevaluated program in the
archive has no metrics, cannot be compared, and would sit in a MAP-Elites cell
it did not earn — worse than not being there.

Off unless `OE_MAX_SEED_FORGE` is set to the number of variants wanted.
"""

from __future__ import annotations

import logging
import os
import tempfile
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

ENV_SEED_FORGE = "OE_MAX_SEED_FORGE"
ENV_EVALUATOR = "EVOLUTION_EVALUATOR_PATH"

# Beyond this the forge is spending real evaluation time before the search has
# made a single request, which is the opposite of the point.
MAX_VARIANTS = 16

# The evaluator's own contract, matching what `sandbox_eval` grants a candidate.
_SUBPROCESS_TIMEOUT_S = 120.0

# Used only when no sandbox backend exists. It mirrors the sandbox script
# deliberately: same module loading, the same flattening of the two shapes an
# evaluator may return, and the same result-through-a-file channel. Divergence
# here would mean a variant scores differently depending on the host it ran on.
_SUBPROCESS_SCRIPT = """
import importlib.util, json, os, sys

_eval_file = {eval_file}
_programs = {programs}
_out = {out}

sys.path.insert(0, os.path.dirname(os.path.abspath(_eval_file)))

_spec = importlib.util.spec_from_file_location("evaluation_module", _eval_file)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)


def _plain(value):
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


_results = []
for _program in _programs:
    try:
        _result = _module.evaluate(_program)
    except Exception as _exc:
        # One bad variant must not cost the batch the other results. The parent
        # reads a null as "this one did not score" and drops it, which is the
        # same outcome it would have reached from a failed single run.
        sys.stderr.write("variant {{!r}} raised {{!r}}\\n".format(_program, _exc))
        _results.append(None)
        continue

    # EvaluationResult carries metrics plus artifacts; a plain dict is metrics
    # only. Both shapes are flattened so the parent has one thing to parse.
    _metrics = getattr(_result, "metrics", None)
    if _metrics is None and isinstance(_result, dict):
        _metrics = _result
    _results.append({{k: _plain(v) for k, v in (_metrics or {{}}).items()}})

with open(_out, "w", encoding="utf-8") as _fh:
    json.dump({{"results": _results}}, _fh)
"""


def requested() -> int:
    try:
        n = int(os.environ.get(ENV_SEED_FORGE, ""))
    except (TypeError, ValueError):
        return 0
    return max(0, min(n, MAX_VARIANTS))


def enabled() -> bool:
    return requested() > 0


class SeedForgeHook:
    """Forges and seeds once per run, on the first program added."""

    def __init__(self) -> None:
        self.done = False
        self.report: Optional[Dict[str, Any]] = None

    def maybe_seed(self, db: Any, program: Any) -> List[Any]:
        """
        Called after the first `add`. Returns the programs it added.

        Identifying the seed by "the database had nothing else in it" rather
        than by generation or parent id: a resumed run loads a populated
        database, and re-forging into it would inject variants of a program the
        search moved past generations ago.
        """
        if self.done or not enabled():
            return []
        self.done = True

        programs = getattr(db, "programs", {}) or {}
        if len(programs) != 1:
            logger.debug("seed forge skipped: database already holds %d programs",
                         len(programs))
            return []

        evaluator = os.environ.get(ENV_EVALUATOR)
        if not evaluator or not os.path.exists(evaluator):
            logger.warning("seed forge is enabled but %s is not set to a readable "
                           "evaluator; skipping rather than adding unscored programs",
                           ENV_EVALUATOR)
            return []

        code = getattr(program, "code", None)
        if not code:
            return []

        added = self._forge_and_add(db, program, code, evaluator)
        logger.info("seed forge added %d variants across %d islands",
                    len(added), len(getattr(db, "islands", []) or []))
        return added

    # -- internals -----------------------------------------------------

    def _forge_and_add(self, db: Any, seed: Any, code: str,
                       evaluator: str) -> List[Any]:
        from oe_max.search.seed_forge import forge
        from openevolve.database import Program

        report = forge(code, max_variants=requested() + 1)
        self.report = report.to_dict()

        islands = len(getattr(db, "islands", []) or []) or 1
        added: List[Program] = []
        # Skip the seed itself: upstream already added it, and adding it again
        # would be a duplicate that the novelty gate has to reject.
        variants = [v for v in report.accepted if v.origin != "seed"]

        chosen = variants[:requested()]
        scores = self._evaluate_all([v.code for v in chosen], evaluator)

        for index, (variant, metrics) in enumerate(zip(chosen, scores)):
            if not metrics:
                continue
            child = Program(
                id=str(uuid.uuid4()), code=variant.code,
                language=getattr(seed, "language", "python"),
                parent_id=getattr(seed, "id", None), generation=0,
                metrics=metrics, iteration_found=0,
                metadata={"seed_forge": True, "forge_origin": variant.origin,
                          "forge_detail": variant.detail},
            )
            try:
                # Round-robin from island 1: island 0 already holds the seed,
                # and stacking the forge there would leave the rest empty —
                # exactly the state this exists to avoid.
                db.add(child, iteration=0,
                       target_island=(index + 1) % islands)
                added.append(child)
            except Exception as exc:
                logger.debug("seed variant rejected by the database: %r", exc)
        return added

    def _evaluate_all(self, codes: List[str], evaluator: str) -> List[Dict[str, float]]:
        """
        Score every variant, preferring the sandbox when it is enabled.

        An unscored variant is dropped rather than added with zeros: a zeroed
        program occupies a MAP-Elites cell it did not earn, and displaces one
        that might have. A dropped variant is `{}` here, and the list is always
        as long as `codes` so the caller can zip it against them.

        The sandbox path stays one call per variant -- its whole purpose is a
        per-variant limit, and batching would make one runaway variant spend
        the whole set's budget. The unsandboxed path batches, because there the
        per-variant process buys no isolation worth 4.6s each.
        """
        from control_plane.telemetry import sandbox_eval
        from oe_max.execution import available_backends

        if not codes:
            return []

        with tempfile.TemporaryDirectory(prefix="oe-max-seed-") as tmp:
            paths = []
            for index, code in enumerate(codes):
                path = os.path.join(tmp, f"variant_{index}.py")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(code)
                paths.append(path)

            # The sandbox is preferred whenever it can actually run. On a host
            # with no POSIX rlimits and no container runtime there is no
            # backend at all, and calling it would fail every variant rather
            # than score it -- which reads as "the forge produced nothing".
            if sandbox_eval.enabled() and available_backends():
                return [self._evaluate_sandboxed(p, evaluator) for p in paths]

            scores = self._evaluate_subprocess_batch(paths, evaluator)
            if scores:
                return scores

            # The batch died as a whole -- a native crash, or the timeout. Fall
            # back to one child per variant, which is what this cost before and
            # is worth paying once it is known to be needed: it recovers every
            # variant except the one that actually killed the process.
            logger.debug("seed batch failed; retrying %d variants individually",
                         len(paths))
            return [self._evaluate_subprocess(p, evaluator) for p in paths]

    def _evaluate_sandboxed(self, path: str, evaluator: str) -> Dict[str, float]:
        """Score one variant under the sandbox, or `{}` if it did not score."""
        from control_plane.telemetry import sandbox_eval

        try:
            payload, result = sandbox_eval.evaluate_in_sandbox(
                evaluator, path, "evaluate", 120.0)
            if result.ok and payload:
                return {
                    k: float(v)
                    for k, v in (payload.get("metrics") or {}).items()
                    if isinstance(v, (int, float, bool))
                }
            logger.debug("seed variant sandbox did not return metrics: %s",
                         result.status)
        except Exception as exc:
            logger.debug("seed variant sandbox failed: %r, falling back "
                         "to subprocess", exc)
        return self._evaluate_subprocess(path, evaluator)

    def _evaluate(self, code: str, evaluator: str) -> Dict[str, float]:
        """Score a single variant. Kept for callers outside the forge loop."""
        scores = self._evaluate_all([code], evaluator)
        return scores[0] if scores else {}

    def _evaluate_subprocess(self, program_path: str,
                             evaluator_path: str) -> Dict[str, float]:
        """Score one variant. Thin wrapper over the batch path."""
        results = self._evaluate_subprocess_batch([program_path], evaluator_path)
        return results[0] if results else {}

    def _evaluate_subprocess_batch(self, program_paths: List[str],
                                   evaluator_path: str) -> List[Dict[str, float]]:
        """
        Score variants in one plain child process, when no sandbox backend exists.

        One child for the whole batch, not one per variant, because the startup
        cost dwarfs the work. Measured on this repo: a bare interpreter is
        0.10s, importing `openevolve` takes it to 2.97s -- almost all of that
        the OpenAI SDK's pydantic model definitions, pulled in transitively by
        `openevolve/__init__` whether or not the child will ever call a model --
        and one evaluation of the example task is 0.10s on top. So per-variant
        children spent 4.6s each to do 0.1s of work, and three variants cost
        14.5s of which 13.7s was Python starting up.

        A variant that raises is reported as `None` by the child and dropped by
        the caller, so one bad variant costs only itself. A child that dies
        outright -- a segfault in a native library, or the batch timeout -- is
        retried one variant at a time by the caller, which restores the old
        isolation exactly when it is needed rather than paying for it always.

        This path enforces a wall clock and nothing else -- no memory ceiling,
        no process-group kill, no network or filesystem restriction. That is a
        real reduction and is named here rather than implied, because the
        difference between this and `evaluate_in_sandbox` is exactly the set of
        guarantees a reader would otherwise assume.

        It is acceptable *here* specifically because Seed Forge mutates the
        operator's own seed program and never runs model output: the code being
        executed is already trusted to the same degree the seed is. Do not
        reuse this for candidate evaluation, where that reasoning does not hold.
        """
        import json
        import subprocess
        import sys

        # Metrics come back through a file, not stdout, for the same reason the
        # sandbox script does it: an evaluator that prints is entirely normal,
        # and mixing the two makes a diagnostic indistinguishable from a
        # measurement.
        if not program_paths:
            return []

        result_path = os.path.join(os.path.dirname(program_paths[0]),
                                   "oe_max_seed_result.json")
        script = _SUBPROCESS_SCRIPT.format(
            eval_file=json.dumps(os.path.abspath(evaluator_path)),
            programs=json.dumps([os.path.abspath(p) for p in program_paths]),
            out=json.dumps(os.path.abspath(result_path)))

        # The wall clock is per variant, so the batch gets the sum. Otherwise
        # batching would tighten the limit every variant runs under, and a set
        # of variants that each scored fine alone would start timing out purely
        # because they were measured together.
        timeout = _SUBPROCESS_TIMEOUT_S * len(program_paths)

        try:
            completed = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.debug("seed batch of %d exceeded %ss in the subprocess fallback",
                         len(program_paths), timeout)
            return []
        except OSError as exc:
            logger.debug("seed variant subprocess could not start: %r", exc)
            return []

        if completed.returncode != 0:
            # Decoded defensively: an evaluator's stderr is arbitrary bytes,
            # and the console encoding is not UTF-8 on every host.
            stderr = (completed.stderr or b"").decode("utf-8", "replace").strip()
            logger.debug("seed variant subprocess failed (exit %d): %s",
                         completed.returncode, stderr[-500:])
            return []

        try:
            with open(result_path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError) as exc:
            logger.debug("seed variant produced no readable result: %r", exc)
            return []

        results = payload.get("results")
        if not isinstance(results, list) or len(results) != len(program_paths):
            logger.debug("seed batch returned %r results for %d variants",
                         type(results).__name__, len(program_paths))
            return []

        return [
            {} if not isinstance(m, dict) else
            {k: float(v) for k, v in m.items() if isinstance(v, (int, float, bool))}
            for m in results
        ]


_hook: Optional[SeedForgeHook] = None
_hook_pid: Optional[int] = None


def get_hook() -> Optional[SeedForgeHook]:
    """
    The process's hook, PID-keyed.

    Seeding happens in the main process, where the database lives. A forked
    worker inheriting a hook that had already marked itself done — or one that
    had not — would either skip or duplicate, and both are wrong.
    """
    global _hook, _hook_pid
    if not enabled():
        return None
    pid = os.getpid()
    if _hook is None or _hook_pid != pid:
        _hook, _hook_pid = SeedForgeHook(), pid
    return _hook


def reset() -> None:
    global _hook, _hook_pid
    _hook, _hook_pid = None, None
