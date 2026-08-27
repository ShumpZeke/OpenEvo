"""
Generic concurrency and budget control — provider-neutral.

Replaces vendor-specific RPM assumptions with:

  max_brain_inflight
  max_eval_workers
  max_test_workers
  generation_budget
  wall_clock_budget
  token_budget where observable
  cost_budget where observable
  candidate_budget
  failure_budget

Adaptive generic backoff for transient host/model failures (no hard-coded
vendor rate-limit contract). Provider-specific throttling lives outside the core.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class BudgetConfig:
    max_brain_inflight: int = 4
    max_eval_workers: int = 4
    max_test_workers: int = 4
    generation_budget: Optional[int] = None
    wall_clock_budget_s: Optional[float] = None
    token_budget: Optional[int] = None
    cost_budget: Optional[float] = None
    candidate_budget: Optional[int] = None
    failure_budget: Optional[int] = None


@dataclass
class BudgetState:
    config: BudgetConfig = field(default_factory=BudgetConfig)
    started_at: float = field(default_factory=time.time)
    candidates_evaluated: int = 0
    failures: int = 0
    tokens_used: int = 0
    cost_used: float = 0.0
    generations: int = 0

    def elapsed_s(self) -> float:
        return time.time() - self.started_at

    def exhausted(self) -> Optional[str]:
        if self.config.candidate_budget is not None and self.candidates_evaluated >= self.config.candidate_budget:
            return "candidate_budget"
        if self.config.failure_budget is not None and self.failures >= self.config.failure_budget:
            return "failure_budget"
        if self.config.wall_clock_budget_s is not None and self.elapsed_s() >= self.config.wall_clock_budget_s:
            return "wall_clock_budget"
        if self.config.token_budget is not None and self.tokens_used >= self.config.token_budget:
            return "token_budget"
        if self.config.cost_budget is not None and self.cost_used >= self.config.cost_budget:
            return "cost_budget"
        if self.config.generation_budget is not None and self.generations >= self.config.generation_budget:
            return "generation_budget"
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "elapsed_s": round(self.elapsed_s(), 1),
            "candidates_evaluated": self.candidates_evaluated,
            "failures": self.failures,
            "tokens_used": self.tokens_used,
            "cost_used": round(self.cost_used, 4),
            "generations": self.generations,
            "exhausted": self.exhausted(),
            "config": self.config.__dict__,
        }


class GenericBackoff:
    """Adaptive backoff for transient host/model failures (retryable)."""

    def __init__(self, base_s: float = 1.0, max_s: float = 60.0, jitter: bool = True) -> None:
        self.base_s = base_s
        self.max_s = max_s
        self.jitter = jitter
        self.attempt = 0

    def next_delay(self) -> float:
        d = min(self.base_s * (2 ** self.attempt), self.max_s)
        self.attempt += 1
        if self.jitter and d > 0:
            d = random.uniform(0, d)
        return d

    def reset(self) -> None:
        self.attempt = 0

    async def sleep(self) -> None:
        await asyncio.sleep(self.next_delay())


class BoundedSemaphore:
    """Thin wrapper so budgets can Gate concurrency without importing asyncio everywhere."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._sem = asyncio.Semaphore(limit)

    async def __aenter__(self):
        await self._sem.acquire()
        return self

    async def __aexit__(self, *_):
        self._sem.release()

    @property
    def available(self) -> int:
        # Semaphore doesn't expose value directly; approximate
        return self.limit
