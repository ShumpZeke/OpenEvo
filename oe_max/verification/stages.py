"""
V1: running the checks.

Two families, and the split matters because one of them needs no task knowledge
at all:

**Generic checks** apply to any candidate in any task, because they test
properties of *measurement* rather than of the answer:

  importable      the code loads at all
  deterministic   two runs under the same seed agree
  finite          the reported score is a real number, not NaN or inf

The middle one is the one that earns its keep. A candidate whose score changes
between identical runs has not improved anything — it has found variance, and
the archive will happily keep the lucky draw. That is invisible to an evaluator
that runs once.

**Declared checks** come from the task's own `verification.py` — properties,
metamorphic relations and randomized trials. See `spec.py`.

Every failure produces a counterexample, because a failing input is worth more
than a failing verdict: it is a permanent test, and it is what the
COUNTEREXAMPLE_REPAIR and ADVERSARIAL_REPAIR operators need in order to be
offered at all.
"""

from __future__ import annotations

import importlib.util
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .counterexamples import Counterexample, CounterexampleStore
from .spec import (
    HIDDEN, METAMORPHIC, PROPERTY, RANDOMIZED, Check, CheckResult,
    VerificationSpec,
)

logger = logging.getLogger(__name__)

GENERIC = "generic"


@dataclass
class VerificationReport:
    candidate_id: Optional[str]
    results: List[CheckResult] = field(default_factory=list)
    counterexamples: List[Counterexample] = field(default_factory=list)
    duration_ms: float = 0.0
    spec_declared: bool = False

    @property
    def passed(self) -> bool:
        """
        A check that *errored* does not fail the candidate.

        A broken check is our bug, not the candidate's, and rejecting a program
        because our own test raised would quietly delete good work. It is
        reported loudly instead — see `errors`.
        """
        return all(r.passed or r.errored for r in self.results)

    @property
    def failures(self) -> List[CheckResult]:
        return [r for r in self.results if not r.passed and not r.errored]

    @property
    def errors(self) -> List[CheckResult]:
        return [r for r in self.results if r.errored]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "passed": self.passed,
            "spec_declared": self.spec_declared,
            "checks_run": len(self.results),
            "failures": [r.to_dict() for r in self.failures],
            "errors": [r.to_dict() for r in self.errors],
            "results": [r.to_dict() for r in self.results],
            "duration_ms": round(self.duration_ms, 2),
        }

    def summary(self) -> str:
        if not self.results:
            return "no checks were run"
        if self.passed and not self.spec_declared:
            return (f"{len(self.results)} generic checks passed; this task "
                    f"declares no verification of its own")
        if self.passed:
            return f"all {len(self.results)} checks passed"
        first = self.failures[0]
        return (f"{len(self.failures)} of {len(self.results)} checks failed; "
                f"first: {first.kind}:{first.name} — {first.message}")


class V1Verifier:
    """
    Property, metamorphic and randomized verification of one candidate.

    Given a spec, or none: with no task-declared checks it still runs the
    generic ones and reports `spec_declared=False`, so a caller can tell
    "verified" from "nothing to verify against".
    """

    def __init__(self, spec: Optional[VerificationSpec] = None,
                 store: Optional[CounterexampleStore] = None,
                 *, entry_point: str = "run_search", seed: int = 0) -> None:
        # `is not None`, not `or`: CounterexampleStore defines __len__, so an
        # empty store is falsy and `store or CounterexampleStore()` silently
        # replaced the caller's store with a fresh one. Every counterexample
        # was then recorded into an object nobody held — the failures were
        # detected correctly and the evidence was thrown away.
        self.spec = spec if spec is not None else VerificationSpec()
        self.store = store if store is not None else CounterexampleStore()
        self.entry_point = entry_point
        self.seed = seed

    # -- entry point ---------------------------------------------------

    def verify(self, code: str, candidate_id: Optional[str] = None,
               reported_score: Optional[float] = None) -> VerificationReport:
        started = time.perf_counter()
        report = VerificationReport(candidate_id=candidate_id,
                                    spec_declared=self.spec.declared)

        module, load_result = self._load(code)
        report.results.append(load_result)
        if module is None:
            report.duration_ms = (time.perf_counter() - started) * 1000.0
            self._record(report)
            return report

        report.results.extend(self._generic_checks(module, reported_score))
        for check in self.spec.of_kind(PROPERTY):
            report.results.append(self._run_check(check, module))
        for check in self.spec.of_kind(METAMORPHIC):
            report.results.append(self._run_check(check, module))
        for check in self.spec.of_kind(HIDDEN):
            report.results.append(self._run_check(check, module))
        for check in self.spec.of_kind(RANDOMIZED):
            report.results.extend(self._run_randomized(check, module))

        report.duration_ms = (time.perf_counter() - started) * 1000.0
        self._record(report)
        return report

    # -- generic checks ------------------------------------------------

    def _generic_checks(self, module: Any,
                        reported_score: Optional[float]) -> List[CheckResult]:
        results = [self._check_deterministic(module)]
        if reported_score is not None:
            results.append(self._check_finite(reported_score))
        return results

    def _check_deterministic(self, module: Any) -> CheckResult:
        """
        Two runs under the same seed must agree.

        A candidate whose result changes between identical runs has not
        improved anything — it has found variance, and an archive that keeps
        the best single draw will keep the lucky one. An evaluator that runs
        once cannot see this at all.
        """
        t0 = time.perf_counter()
        entry = getattr(module, self.entry_point, None)
        if entry is None:
            return CheckResult(
                "deterministic", GENERIC, True,
                f"no {self.entry_point}() to call; skipped",
                duration_ms=(time.perf_counter() - t0) * 1000.0)
        try:
            first = self._seeded_call(entry)
            second = self._seeded_call(entry)
        except Exception as exc:
            return CheckResult(
                "deterministic", GENERIC, False,
                f"{self.entry_point}() raised: {exc}", errored=False,
                duration_ms=(time.perf_counter() - t0) * 1000.0)

        agree = _approx_equal(first, second)
        return CheckResult(
            "deterministic", GENERIC, agree,
            "" if agree else
            "two runs with the same seed disagreed, so the reported score is "
            "a draw from a distribution rather than a measurement",
            inputs={"seed": self.seed}, expected=_short(first), actual=_short(second),
            duration_ms=(time.perf_counter() - t0) * 1000.0)

    def _check_finite(self, score: float) -> CheckResult:
        ok = isinstance(score, (int, float)) and math.isfinite(score)
        return CheckResult(
            "finite_score", GENERIC, ok,
            "" if ok else f"reported score is not a finite number: {score!r}",
            actual=score)

    def _seeded_call(self, entry: Callable) -> Any:
        """Call the entry point with every RNG we can reach pinned."""
        import random

        random.seed(self.seed)
        try:
            import numpy as np

            np.random.seed(self.seed)
        except ImportError:
            pass
        return entry()

    # -- declared checks -----------------------------------------------

    def _run_check(self, check: Check, module: Any, inputs: Any = None) -> CheckResult:
        t0 = time.perf_counter()
        try:
            outcome = check.fn(module) if inputs is None else check.fn(module, inputs)
        except AssertionError as exc:
            return CheckResult(check.name, check.kind, False, str(exc) or "assertion failed",
                               inputs=inputs,
                               duration_ms=(time.perf_counter() - t0) * 1000.0)
        except Exception as exc:
            # The check itself is broken. Reported, never counted against the
            # candidate: rejecting a program because our test raised would
            # quietly delete good work.
            return CheckResult(check.name, check.kind, False,
                               f"check raised {type(exc).__name__}: {exc}",
                               inputs=inputs, errored=True,
                               duration_ms=(time.perf_counter() - t0) * 1000.0)

        passed, message = _interpret(outcome)
        return CheckResult(check.name, check.kind, passed, message, inputs=inputs,
                           duration_ms=(time.perf_counter() - t0) * 1000.0)

    def _run_randomized(self, check: Check, module: Any) -> List[CheckResult]:
        """
        The same check under generated inputs, stopping at the first failure.

        Stopping early is deliberate: one counterexample is enough to reject
        the candidate, and continuing would fill the store with variations of
        a failure already recorded.
        """
        if self.spec.generator is None:
            return [CheckResult(
                check.name, RANDOMIZED, True,
                "no generate_input() declared; randomized trials skipped")]
        results: List[CheckResult] = []
        for trial in range(check.trials):
            try:
                inputs = self.spec.generator(trial)
            except Exception as exc:
                results.append(CheckResult(check.name, RANDOMIZED, False,
                                           f"generate_input raised: {exc}",
                                           errored=True))
                break
            result = self._run_check(check, module, inputs)
            results.append(result)
            if not result.passed:
                break
        return results

    # -- loading -------------------------------------------------------

    def _load(self, code: str) -> Any:
        t0 = time.perf_counter()
        try:
            spec = importlib.util.spec_from_loader("evolution_candidate", loader=None)
            module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
            exec(compile(code, "<candidate>", "exec"), module.__dict__)
        except Exception as exc:
            return None, CheckResult(
                "importable", GENERIC, False,
                f"candidate does not load: {type(exc).__name__}: {exc}",
                duration_ms=(time.perf_counter() - t0) * 1000.0)
        return module, CheckResult("importable", GENERIC, True,
                                   duration_ms=(time.perf_counter() - t0) * 1000.0)

    def _record(self, report: VerificationReport) -> None:
        for failure in report.failures:
            ce = Counterexample(
                check=f"{failure.kind}:{failure.name}", inputs=failure.inputs,
                expected=failure.expected, actual=failure.actual,
                message=failure.message, candidate_id=report.candidate_id,
            )
            report.counterexamples.append(self.store.add(ce))


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _interpret(outcome: Any) -> Any:
    """
    Read what a check returned.

    A check may `assert`, return a bool, or return `(bool, message)`. Returning
    None is treated as passing, so a check written entirely with asserts — the
    most natural way — works without ceremony.
    """
    if outcome is None or outcome is True:
        return True, ""
    if outcome is False:
        return False, "check returned False"
    if isinstance(outcome, tuple) and len(outcome) == 2:
        return bool(outcome[0]), str(outcome[1])
    return bool(outcome), ""


def _approx_equal(a: Any, b: Any, tol: float = 1e-9) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if isinstance(a, bool) or isinstance(b, bool):
            return a == b
        if math.isnan(a) and math.isnan(b):
            return True
        return math.isclose(a, b, rel_tol=tol, abs_tol=tol)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_approx_equal(x, y, tol) for x, y in zip(a, b))
    try:
        return bool(a == b)
    except Exception:
        return False


def _short(value: Any, limit: int = 200) -> Any:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "…"
