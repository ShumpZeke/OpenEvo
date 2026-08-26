"""
Running V1 verification during a live run, on the candidates worth the cost.

Verifying everything would trade away the throughput the rest of the system
exists to buy. Verifying nothing means the first candidate that games the
metric becomes the champion, and every generation after it is built on that.

So it runs on exactly two kinds of candidate, and both are chosen for a reason:

* **A new champion.** Whatever the champion is, the run is now optimising
  around it. If it is wrong, everything downstream is wrong, and the cost of
  finding out later is the whole run.
* **A suspicious jump.** A score that leaps far beyond this run's own history
  of improvements is the exact shape of a candidate that stopped solving the
  problem and started reporting a number. See `suspicion.py` for why that is
  measured with median and MAD rather than mean and standard deviation.

Failing verification does **not** remove the candidate from the population.
That is deliberate: this is instrumentation, and instrumentation that silently
deletes the engine's work would make the fork's behaviour differ from upstream
in a way no test would catch. It emits `candidate.verification.failed` with the
counterexample, which the Control Center surfaces and an operator can act on.
Enforcement is a separate decision, and an explicit one.

Off unless `OE_MAX_VERIFY` is set.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

ENV_VERIFY = "OE_MAX_VERIFY"
ENV_EVALUATOR = "EVOLUTION_EVALUATOR_PATH"
ENV_ENTRY_POINT = "OE_MAX_VERIFY_ENTRY_POINT"


def enabled() -> bool:
    return os.environ.get(ENV_VERIFY, "").lower() in ("1", "true", "yes", "on")


class LiveVerifier:
    """Holds the spec, the counterexample store and the suspicion history."""

    def __init__(self, evaluator_path: Optional[str] = None,
                 entry_point: Optional[str] = None,
                 store_path: Optional[str] = None) -> None:
        from oe_max.verification import (
            CounterexampleStore, SuspicionDetector, V1Verifier, load_spec,
        )

        self.evaluator_path = evaluator_path or os.environ.get(ENV_EVALUATOR)
        self.entry_point = (entry_point
                            or os.environ.get(ENV_ENTRY_POINT)
                            or "run_search")
        self.spec = (load_spec(self.evaluator_path) if self.evaluator_path
                     else load_spec(""))
        self.store = CounterexampleStore(store_path)
        self.detector = SuspicionDetector()
        self.verifier = V1Verifier(self.spec, self.store,
                                   entry_point=self.entry_point)
        self._verified: set = set()

    def should_verify(self, *, is_new_best: bool,
                      delta: Optional[float]) -> Dict[str, Any]:
        """
        Decide, and say why either way.

        The reason is returned rather than logged because it ends up on the
        event: "this was not verified" is only useful next to "because it was
        neither a champion nor an unusual jump".
        """
        verdict = self.detector.check(delta)
        if is_new_best:
            return {"verify": True, "trigger": "new_champion",
                    "reason": "a new champion is what the run now optimises around",
                    "suspicion": verdict.to_dict()}
        if verdict.suspicious:
            return {"verify": True, "trigger": "suspicious_jump",
                    "reason": verdict.reason, "suspicion": verdict.to_dict()}
        return {"verify": False, "trigger": None,
                "reason": "neither a new champion nor an unusual jump",
                "suspicion": verdict.to_dict()}

    def verify(self, code: str, candidate_id: str,
               reported_score: Optional[float] = None) -> Any:
        """Verify once per candidate; a champion re-confirmed is not re-run."""
        if candidate_id in self._verified:
            return None
        self._verified.add(candidate_id)
        return self.verifier.verify(code, candidate_id, reported_score)


_active: Optional[LiveVerifier] = None
_active_pid: Optional[int] = None


def get_verifier() -> Optional[LiveVerifier]:
    """
    The process's verifier, built on first use.

    PID-keyed for the same reason the bus is: a forked worker inherits this
    module's globals, and a verifier built in the parent carries the parent's
    suspicion history. Verification runs in the main process — where `add`
    happens — so a child that somehow reached this would be judging against
    someone else's distribution.
    """
    global _active, _active_pid
    if not enabled():
        return None
    pid = os.getpid()
    if _active is not None and _active_pid == pid:
        return _active
    try:
        _active = LiveVerifier()
        _active_pid = pid
    except Exception as exc:
        logger.warning("verification is enabled but could not start: %r", exc)
        return None
    return _active


def reset() -> None:
    """Drop the process verifier. For tests, and after a fork."""
    global _active, _active_pid
    _active, _active_pid = None, None
