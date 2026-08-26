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

        for index, variant in enumerate(variants[:requested()]):
            metrics = self._evaluate(variant.code, evaluator)
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

    def _evaluate(self, code: str, evaluator: str) -> Dict[str, float]:
        """
        Score one variant, preferring the sandbox when it is enabled.

        An unscored variant is dropped rather than added with zeros: a zeroed
        program occupies a MAP-Elites cell it did not earn, and displaces one
        that might have.
        """
        from control_plane.telemetry import sandbox_eval

        with tempfile.TemporaryDirectory(prefix="oe-max-seed-") as tmp:
            path = os.path.join(tmp, "variant.py")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(code)
            try:
                payload, result = sandbox_eval.evaluate_in_sandbox(
                    evaluator, path, "evaluate", 120.0)
            except Exception as exc:
                logger.debug("seed variant evaluation failed: %r", exc)
                return {}

        if not result.ok or not payload:
            logger.debug("seed variant did not evaluate: %s", result.status)
            return {}
        return {k: float(v) for k, v in (payload.get("metrics") or {}).items()
                if isinstance(v, (int, float, bool))}


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
