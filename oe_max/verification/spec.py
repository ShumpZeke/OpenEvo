"""
How a task declares what "honest" means for it.

A property is task knowledge: "the point you returned must lie inside the
bounds you were given" is meaningful for function minimisation and meaningless
for a sorting task. So the checks live next to the task, in a `verification.py`
beside its evaluator, and this module is only the shape they take.

The distinction the spec keeps
------------------------------

* **Property** — must hold for every run of a correct program. A violation is a
  bug, full stop.
* **Metamorphic** — relates two runs to each other ("with the search space
  restricted, you cannot return a point outside it"). These catch the cheats
  properties miss, because a program that ignores its inputs can satisfy every
  single-run property and still be wrong.
* **Randomized** — the same checks under many generated inputs. This is where
  a program that works on the evaluator's fixed cases and nowhere else gets
  caught.

A task with no `verification.py` gets the generic checks only, and says so.
Silently reporting "verified" for a task that declared nothing would be worse
than not verifying at all.
"""

from __future__ import annotations

import importlib.util
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Kinds of check, in the order they are cheapest to run.
PROPERTY = "property"
METAMORPHIC = "metamorphic"
RANDOMIZED = "randomized"
HIDDEN = "hidden"


@dataclass
class CheckResult:
    """What one check decided, and the input that decided it."""

    name: str
    kind: str
    passed: bool
    message: str = ""
    inputs: Any = None
    expected: Any = None
    actual: Any = None
    duration_ms: float = 0.0
    errored: bool = False        # the check itself blew up, distinct from failing

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "passed": self.passed,
                "errored": self.errored, "message": self.message,
                "inputs": self.inputs, "expected": self.expected,
                "actual": self.actual, "duration_ms": round(self.duration_ms, 2)}


@dataclass
class Check:
    """One named check over a loaded candidate module."""

    name: str
    kind: str
    fn: Callable[..., Any]
    # Randomized checks are given generated inputs; the rest are given none.
    trials: int = 1

    def describe(self) -> str:
        return f"{self.kind}:{self.name}"


@dataclass
class VerificationSpec:
    """The checks a task declares, plus how to generate inputs for them."""

    checks: List[Check] = field(default_factory=list)
    generator: Optional[Callable[[int], Any]] = None
    source: Optional[str] = None
    # Reported rather than inferred: a caller must be able to tell "this task
    # declared nothing" from "this task declared checks and they all passed".
    declared: bool = False

    def of_kind(self, kind: str) -> List[Check]:
        return [c for c in self.checks if c.kind == kind]

    def summary(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "declared": self.declared,
            "counts": {k: len(self.of_kind(k))
                       for k in (PROPERTY, METAMORPHIC, RANDOMIZED, HIDDEN)},
            "has_generator": self.generator is not None,
        }


def spec_path_for(evaluator_path: str) -> str:
    """Where a task's verification module lives: beside its evaluator."""
    return os.path.join(os.path.dirname(os.path.abspath(evaluator_path)),
                        "verification.py")


def load_spec(evaluator_path: str) -> VerificationSpec:
    """
    Load a task's `verification.py`, if it has one.

    A module that declares nothing, fails to import, or raises on load yields an
    undeclared spec rather than an exception. Verification is a safety net; a
    broken net must not also be a broken run.
    """
    path = spec_path_for(evaluator_path)
    if not os.path.exists(path):
        return VerificationSpec(source=None, declared=False)

    try:
        module = _load_module(path)
    except Exception as exc:
        logger.warning("verification spec at %s failed to load: %r", path, exc)
        return VerificationSpec(source=path, declared=False)

    checks: List[Check] = []
    for kind, prefix in ((PROPERTY, "property_"), (METAMORPHIC, "metamorphic_"),
                         (RANDOMIZED, "randomized_"), (HIDDEN, "hidden_")):
        for name in sorted(dir(module)):
            if not name.startswith(prefix):
                continue
            fn = getattr(module, name)
            if not callable(fn):
                continue
            checks.append(Check(
                name=name[len(prefix):] or name, kind=kind, fn=fn,
                trials=int(getattr(fn, "trials", 25 if kind == RANDOMIZED else 1)),
            ))

    generator = getattr(module, "generate_input", None)
    return VerificationSpec(
        checks=checks, generator=generator if callable(generator) else None,
        source=path, declared=bool(checks),
    )


def _load_module(path: str):
    spec = importlib.util.spec_from_file_location(
        f"evolution_verification_{abs(hash(path))}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
