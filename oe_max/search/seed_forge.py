"""
Seed Forge: start from a population, not from one program.

Upstream begins every run with a single seed and mutates outward from it. That
makes the first few generations expensive in a specific way: the search has to
spend model requests discovering the shape of the space before it can start
improving anything, and every island begins in the same basin — so the island
structure, which exists to keep populations apart, starts with nothing to keep
apart.

A forge produces a starting population by transforming the seed *without a
model request*, which is the point: these variants are free. They are not
better programs and are not meant to be. They are cheap, valid, structurally
distinct points to begin from.

What it will and will not do
----------------------------

The transformations are deliberately dull, because a clever one that produces
subtly broken programs is worse than no forge at all — it would spend the first
generation of every island on candidates that cannot run.

  scale numeric literals   0.5x, 2x, 10x on the numbers a program tunes
  vary keyword defaults    the iteration counts and bounds in a signature

Both are AST rewrites over literals, so a variant either parses or the
transformation is discarded. Nothing here reorders statements, swaps operators
or touches control flow: those produce plausible-looking programs that are
wrong, which is exactly what the evaluator cannot tell you cheaply.

Every variant then goes through the same gates a mutated candidate does — G0
validity and G1 deduplication — so a transformation that produced the original
back, or produced something that will not compile, never reaches the
population.
"""

from __future__ import annotations

import ast
import copy
import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..evaluation.gates import DedupIndex, GateResult, g0_validity

# Multipliers applied to numeric literals. 1.0 is excluded because it is the
# original; the rest span an order of magnitude in each direction, which is
# where a parameter that matters usually shows an effect.
DEFAULT_SCALES: Tuple[float, ...] = (0.5, 2.0, 10.0)

# Literals never rewritten. 0 and 1 are almost always structural — indices,
# increments, identity elements — and scaling them changes what a program
# *does* rather than how hard it tries.
_STRUCTURAL_VALUES = (0, 1, -1, 0.0, 1.0, -1.0)

# Keyword arguments whose defaults are worth varying by name: they are the
# effort dials of a search, and a run that only ever tries one is not exploring.
EFFORT_KEYWORDS = ("iterations", "n_iter", "steps", "max_iter", "max_steps",
                   "samples", "restarts", "population", "trials")


@dataclass
class Variant:
    """One forged starting point, and how it was made."""

    code: str
    origin: str                       # which transformation produced it
    detail: str = ""
    rejected: Optional[str] = None    # why it did not make the cut, if it did not

    @property
    def accepted(self) -> bool:
        return self.rejected is None

    def fingerprint(self) -> str:
        return hashlib.sha256(self.code.encode()).hexdigest()[:12]

    def to_dict(self) -> Dict[str, Any]:
        return {"origin": self.origin, "detail": self.detail,
                "accepted": self.accepted, "rejected": self.rejected,
                "fingerprint": self.fingerprint(), "length": len(self.code)}


@dataclass
class ForgeReport:
    """What the forge produced, and what it threw away."""

    seed_fingerprint: str
    variants: List[Variant] = field(default_factory=list)

    @property
    def accepted(self) -> List[Variant]:
        return [v for v in self.variants if v.accepted]

    def to_dict(self) -> Dict[str, Any]:
        rejected: Dict[str, int] = {}
        for v in self.variants:
            if v.rejected:
                rejected[v.rejected] = rejected.get(v.rejected, 0) + 1
        return {
            "seed": self.seed_fingerprint,
            "produced": len(self.variants),
            "accepted": len(self.accepted),
            # Reported rather than swallowed: a forge whose variants are nearly
            # all duplicates is doing nothing, and the only way to notice is
            # for it to say so.
            "rejected_by": rejected,
            "variants": [v.to_dict() for v in self.variants],
        }

    def summary(self) -> str:
        if not self.variants:
            return "no variants produced; the seed has nothing to vary"
        if not self.accepted:
            return (f"all {len(self.variants)} variants were rejected — the "
                    f"forge added nothing to this seed")
        return (f"{len(self.accepted)} of {len(self.variants)} variants accepted "
                f"from seed {self.seed_fingerprint}")


class _LiteralScaler(ast.NodeTransformer):
    """Multiply numeric literals, leaving structural ones alone."""

    def __init__(self, scale: float) -> None:
        self.scale = scale
        self.changed = 0

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        value = node.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return node
        if value in _STRUCTURAL_VALUES:
            return node
        scaled = value * self.scale
        if isinstance(value, int):
            scaled = int(round(scaled))
            # A scale that collapses an int to a structural value would change
            # the program's meaning rather than its effort.
            if scaled in _STRUCTURAL_VALUES:
                return node
        if scaled == value:
            return node
        self.changed += 1
        return ast.copy_location(ast.Constant(value=scaled), node)


class _KeywordDefaultScaler(ast.NodeTransformer):
    """Vary the effort dials in a function signature, by name."""

    def __init__(self, scale: float, names: Sequence[str]) -> None:
        self.scale = scale
        self.names = set(names)
        self.changed = 0

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        args = node.args
        # Defaults align with the *last* N positional arguments, which is the
        # detail that makes this correct rather than approximately correct.
        positional = args.posonlyargs + args.args
        offset = len(positional) - len(args.defaults)
        for index, default in enumerate(args.defaults):
            name = positional[offset + index].arg
            if name in self.names:
                args.defaults[index] = self._scaled(default)
        for kwarg, default in zip(args.kwonlyargs, args.kw_defaults):
            if default is not None and kwarg.arg in self.names:
                args.kw_defaults[args.kwonlyargs.index(kwarg)] = self._scaled(default)
        self.generic_visit(node)
        return node

    def _scaled(self, node: ast.AST) -> ast.AST:
        if not isinstance(node, ast.Constant):
            return node
        value = node.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return node
        scaled = int(round(value * self.scale)) if isinstance(value, int) \
            else value * self.scale
        if scaled == value or scaled <= 0:
            return node
        self.changed += 1
        return ast.copy_location(ast.Constant(value=scaled), node)


def _render(tree: ast.AST) -> Optional[str]:
    try:
        return ast.unparse(ast.fix_missing_locations(tree))
    except Exception:
        # A transformation that produced an unrenderable tree is discarded
        # rather than debugged: variants are free, and one lost costs nothing.
        return None


def forge(seed_code: str, *,
          scales: Sequence[float] = DEFAULT_SCALES,
          effort_keywords: Sequence[str] = EFFORT_KEYWORDS,
          required_functions: Optional[List[str]] = None,
          max_variants: Optional[int] = None,
          index: Optional[DedupIndex] = None) -> ForgeReport:
    """
    Build a starting population from one seed.

    The seed itself is always the first accepted variant: a forge that replaced
    the program the operator supplied would be making a decision nobody asked
    for.
    """
    report = ForgeReport(
        seed_fingerprint=hashlib.sha256(seed_code.encode()).hexdigest()[:12])

    try:
        base = ast.parse(seed_code)
    except SyntaxError as exc:
        report.variants.append(Variant(seed_code, "seed", rejected=f"seed does not parse: {exc}"))
        return report

    dedup = index if index is not None else DedupIndex()
    candidates: List[Variant] = [Variant(seed_code, "seed", "unchanged")]

    for scale in scales:
        scaler = _LiteralScaler(scale)
        tree = scaler.visit(copy.deepcopy(base))
        if scaler.changed:
            code = _render(tree)
            if code:
                candidates.append(Variant(
                    code, "scale_literals", f"x{scale} over {scaler.changed} literals"))

        kw = _KeywordDefaultScaler(scale, effort_keywords)
        tree = kw.visit(copy.deepcopy(base))
        if kw.changed:
            code = _render(tree)
            if code:
                candidates.append(Variant(
                    code, "scale_effort", f"x{scale} over {kw.changed} defaults"))

    for variant in candidates:
        gate = g0_validity(variant.code, required_functions=required_functions)
        if gate.result is GateResult.REJECT:
            variant.rejected = f"G0: {gate.reason}"
            report.variants.append(variant)
            continue
        dup = dedup.check(variant.code, use_structural=False)
        if dup.result is GateResult.REJECT:
            # Expected and worth counting: scaling a program with no numbers
            # left to scale reproduces it exactly.
            variant.rejected = f"G1: {dup.reason}"
            report.variants.append(variant)
            continue
        dedup.add(variant.code, variant.fingerprint(), use_structural=False)
        report.variants.append(variant)
        if max_variants and len(report.accepted) >= max_variants:
            break

    return report
