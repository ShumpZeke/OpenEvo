"""
Cheap gates: G0 (validity) and G1 (deduplication).

The cascade's guiding rule is "weak candidates must die cheaply". These two
stages cost microseconds and run before anything that costs a model call, a
sandbox, or a benchmark.

The economics are stark with the measured primary route: Ox Alpha averaged
~130 seconds and ~8,000 tokens per generation. Every candidate rejected here is
a benchmark run and an evaluator invocation not spent — and, for duplicates,
a candidate that could never have improved the archive anyway.

Four dedup strengths, each catching what the previous misses:

  exact        byte-identical
  normalized   differs only in whitespace/blank lines
  ast          differs only in comments, docstrings, formatting
  structural   differs only in local identifier names

They are ordered by cost and by strictness. Structural equivalence is the
strongest claim and the easiest to get wrong, so it is opt-in per call rather
than always-on.
"""

from __future__ import annotations

import ast
import hashlib
import io
import re
import tokenize
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple


class GateResult(str, Enum):
    PASS = "pass"
    REJECT = "reject"


@dataclass
class GateOutcome:
    gate: str
    result: GateResult
    reason: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)
    cost_us: float = 0.0

    @property
    def passed(self) -> bool:
        return self.result is GateResult.PASS

    def to_dict(self) -> Dict[str, Any]:
        return {"gate": self.gate, "result": self.result.value,
                "reason": self.reason, "detail": self.detail}


# --------------------------------------------------------------------------
# G0 — validity
# --------------------------------------------------------------------------


def g0_validity(
    code: str,
    *,
    required_functions: Optional[List[str]] = None,
    required_imports: Optional[List[str]] = None,
    forbidden_imports: Optional[List[str]] = None,
    max_length: Optional[int] = None,
) -> GateOutcome:
    """
    Parse, syntax, interface and import checks.

    `forbidden_imports` is a *hygiene* check, not a security boundary. A
    candidate that wants to evade it trivially can (`__import__`, `exec`).
    Real containment is the sandbox's job; this only catches accidents early
    and cheaply.
    """
    if not code or not code.strip():
        return GateOutcome("G0", GateResult.REJECT, "empty program")

    if max_length is not None and len(code) > max_length:
        return GateOutcome("G0", GateResult.REJECT,
                           f"program too long: {len(code)} > {max_length}",
                           {"length": len(code)})

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return GateOutcome("G0", GateResult.REJECT,
                           f"syntax error line {e.lineno}: {e.msg}",
                           {"lineno": e.lineno, "msg": e.msg})
    except (ValueError, RecursionError) as e:
        # Deeply nested or null-byte-bearing sources can fail here rather than
        # raising SyntaxError.
        return GateOutcome("G0", GateResult.REJECT,
                           f"unparseable: {type(e).__name__}: {e}")

    defined = {n.name for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    missing = [f for f in (required_functions or []) if f not in defined]
    if missing:
        return GateOutcome("G0", GateResult.REJECT,
                           f"missing required function(s): {', '.join(missing)}",
                           {"defined": sorted(defined), "missing": missing})

    imported = _imported_modules(tree)
    missing_imports = [m for m in (required_imports or []) if m not in imported]
    if missing_imports:
        return GateOutcome("G0", GateResult.REJECT,
                           f"missing required import(s): {', '.join(missing_imports)}",
                           {"imported": sorted(imported)})

    banned = sorted(imported & set(forbidden_imports or []))
    if banned:
        return GateOutcome("G0", GateResult.REJECT,
                           f"forbidden import(s): {', '.join(banned)}",
                           {"forbidden": banned})

    return GateOutcome("G0", GateResult.PASS, "valid",
                       {"functions": sorted(defined), "imports": sorted(imported)})


def _imported_modules(tree: ast.AST) -> Set[str]:
    mods: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                mods.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                mods.add(node.module.split(".")[0])
    return mods


# --------------------------------------------------------------------------
# G1 — deduplication
# --------------------------------------------------------------------------


def exact_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()[:32]


def normalized_hash(code: str) -> str:
    """Insensitive to trailing whitespace, blank lines and line endings."""
    lines = [ln.rstrip() for ln in code.replace("\r\n", "\n").split("\n")]
    return hashlib.sha256("\n".join(ln for ln in lines if ln.strip()).encode()).hexdigest()[:32]


def ast_hash(code: str) -> Optional[str]:
    """
    Structure-only hash: comments, docstrings and formatting are ignored.

    Returns None for unparseable code so callers can distinguish "no AST" from
    "different AST" rather than treating a parse failure as a novel structure.
    """
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, RecursionError):
        return None
    return hashlib.sha256(ast.dump(_strip_docstrings(tree), include_attributes=False)
                          .encode()).hexdigest()[:32]


def structural_hash(code: str) -> Optional[str]:
    """
    Alpha-renaming hash: identical up to local variable naming.

    Only *local* names are canonicalised. Renaming module-level functions,
    attributes or imported names would collapse genuinely different programs,
    so those are preserved.
    """
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, RecursionError):
        return None
    tree = _strip_docstrings(tree)
    _AlphaRenamer().visit(tree)
    return hashlib.sha256(ast.dump(tree, include_attributes=False).encode()).hexdigest()[:32]


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Module)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return tree


class _AlphaRenamer(ast.NodeTransformer):
    """Canonicalise local names to v0, v1, … per function scope."""

    def __init__(self) -> None:
        self._scopes: List[Dict[str, str]] = [{}]

    def _canon(self, name: str) -> str:
        scope = self._scopes[-1]
        if name not in scope:
            scope[name] = f"v{len(scope)}"
        return scope[name]

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        self._scopes.append({})
        for arg in list(node.args.args) + list(node.args.kwonlyargs):
            arg.arg = self._canon(arg.arg)
        node.body = [self.visit(n) for n in node.body]
        self._scopes.pop()
        return node

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Name(self, node: ast.Name) -> ast.AST:
        # Only rename names bound in the current function scope; globals,
        # builtins and imports keep their identity.
        if len(self._scopes) > 1:
            if isinstance(node.ctx, ast.Store) or node.id in self._scopes[-1]:
                node.id = self._canon(node.id)
        return node


@dataclass
class DedupIndex:
    """Tracks seen programs at four strengths."""

    exact: Dict[str, str] = field(default_factory=dict)
    normalized: Dict[str, str] = field(default_factory=dict)
    ast: Dict[str, str] = field(default_factory=dict)
    structural: Dict[str, str] = field(default_factory=dict)
    hits: Dict[str, int] = field(default_factory=lambda: {
        "exact": 0, "normalized": 0, "ast": 0, "structural": 0})

    def check(self, code: str, *, use_structural: bool = False) -> GateOutcome:
        """Cheapest-first; the first hit wins and stops the work."""
        h = exact_hash(code)
        if h in self.exact:
            self.hits["exact"] += 1
            return GateOutcome("G1", GateResult.REJECT, "exact duplicate",
                               {"kind": "exact", "of": self.exact[h]})

        n = normalized_hash(code)
        if n in self.normalized:
            self.hits["normalized"] += 1
            return GateOutcome("G1", GateResult.REJECT,
                               "duplicate ignoring whitespace",
                               {"kind": "normalized", "of": self.normalized[n]})

        a = ast_hash(code)
        if a is not None and a in self.ast:
            self.hits["ast"] += 1
            return GateOutcome("G1", GateResult.REJECT,
                               "duplicate ignoring comments and formatting",
                               {"kind": "ast", "of": self.ast[a]})

        s = None
        if use_structural:
            s = structural_hash(code)
            if s is not None and s in self.structural:
                self.hits["structural"] += 1
                return GateOutcome("G1", GateResult.REJECT,
                                   "duplicate up to local variable renaming",
                                   {"kind": "structural", "of": self.structural[s]})

        return GateOutcome("G1", GateResult.PASS, "novel",
                           {"exact": h, "normalized": n, "ast": a, "structural": s})

    def add(self, code: str, candidate_id: str, *, use_structural: bool = True) -> None:
        self.exact.setdefault(exact_hash(code), candidate_id)
        self.normalized.setdefault(normalized_hash(code), candidate_id)
        a = ast_hash(code)
        if a is not None:
            self.ast.setdefault(a, candidate_id)
        if use_structural:
            s = structural_hash(code)
            if s is not None:
                self.structural.setdefault(s, candidate_id)

    def stats(self) -> Dict[str, Any]:
        return {
            "unique_exact": len(self.exact),
            "unique_normalized": len(self.normalized),
            "unique_ast": len(self.ast),
            "unique_structural": len(self.structural),
            "duplicate_hits": dict(self.hits),
            "total_duplicates": sum(self.hits.values()),
        }


def g1_dedup(code: str, index: DedupIndex, *, use_structural: bool = False) -> GateOutcome:
    return index.check(code, use_structural=use_structural)
