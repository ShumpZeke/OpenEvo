"""
Staged evaluation funnel — weak candidates die cheaply.

Pipeline:
  candidate
  -> parse/apply validation (G0)
  -> exact / normalized / AST / structural dedup (G1)
  -> static checks (lint/type where configured)
  -> dependency-impact analysis
  -> small affected test subset
  -> cheap benchmark
  -> fitness/novelty estimate
  -> full relevant tests
  -> expensive benchmark
  -> semantic/LLM judge only when required
  -> archive/selection

Do not use an LLM to evaluate what deterministic tooling can determine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class Stage(str, Enum):
    G0_VALIDITY = "g0_validity"
    G1_DEDUP = "g1_dedup"
    STATIC_CHECKS = "static_checks"
    IMPACT = "impact"
    AFFECTED_TESTS = "affected_tests"
    CHEAP_BENCHMARK = "cheap_benchmark"
    FITNESS_ESTIMATE = "fitness_estimate"
    FULL_TESTS = "full_tests"
    EXPENSIVE_BENCHMARK = "expensive_benchmark"
    SEMANTIC_JUDGE = "semantic_judge"
    ARCHIVE = "archive"


@dataclass
class FunnelResult:
    passed: bool
    stage: Stage
    reason: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)
    cost_ms: float = 0.0


StageFn = Callable[[str, Dict[str, Any]], FunnelResult]


@dataclass
class FunnelConfig:
    enabled_stages: List[Stage] = field(
        default_factory=lambda: [
            Stage.G0_VALIDITY,
            Stage.G1_DEDUP,
            Stage.STATIC_CHECKS,
            Stage.AFFECTED_TESTS,
            Stage.CHEAP_BENCHMARK,
            Stage.FULL_TESTS,
            Stage.ARCHIVE,
        ]
    )
    # Gate thresholds
    dedup_strength: str = "ast"  # exact | normalized | ast | structural
    # Successive halving: escalating budgets
    halving_keep_ratio: float = 0.33
    semantic_judge_only_when_required: bool = True


class Funnel:
    """Runs stages in order; first failure stops the funnel (cheap death)."""

    def __init__(self, config: Optional[FunnelConfig] = None) -> None:
        self.config = config or FunnelConfig()
        self._stages: Dict[Stage, StageFn] = {}

    def register(self, stage: Stage, fn: StageFn) -> None:
        self._stages[stage] = fn

    def run(self, code: str, ctx: Optional[Dict[str, Any]] = None) -> List[FunnelResult]:
        ctx = ctx or {}
        results: List[FunnelResult] = []
        for stage in self.config.enabled_stages:
            fn = self._stages.get(stage)
            if fn is None:
                # No-op pass when stage not registered (e.g., no linter configured)
                results.append(FunnelResult(passed=True, stage=stage, reason="not configured — pass"))
                continue
            res = fn(code, ctx)
            results.append(res)
            if not res.passed:
                break
        return results

    def passed(self, results: List[FunnelResult]) -> bool:
        return all(r.passed for r in results)

    def failed_at(self, results: List[FunnelResult]) -> Optional[Stage]:
        for r in results:
            if not r.passed:
                return r.stage
        return None
