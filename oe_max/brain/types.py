"""
Provider-neutral request/response types.

A BrainRequest describes *work*, not *vendor*.

It must never require a vendor-specific model identifier.
Provider/model identity may be recorded as metadata for reproducibility
but must NOT determine routing inside the evolutionary core.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class PolicyMode(str, Enum):
    """
    Cognitive operations, not providers.

    Convert old hardcoded role→model concepts to prompt/policy modes
    executed by ONE inherited OpenCode model (brain.mode = inherit).

    Old:
      EVOLVER -> <provider-a>/<model-a>, CRITIC -> <provider-b>/<model-b> ...

    New:
      PolicyMode.MUTATION_GENERATION -> same OpenCode-selected model
      PolicyMode.ADVERSARIAL_REVIEW  -> same model, different prompt policy
    """

    MUTATION_GENERATION = "mutation-generation"   # EVOLVER
    ADVERSARIAL_REVIEW = "adversarial-review"     # CRITIC
    SEARCH_PLANNING = "search-planning"           # PLANNER
    EXPERIMENT_ANALYSIS = "experiment-analysis"    # ANALYST
    ARCHITECTURE_MUTATION = "architecture-mutation"  # ARCHITECT
    RESEARCH = "research"                         # RESEARCHER
    CODE_REVIEW = "code-review"                   # REVIEWER
    EVALUATION = "evaluation"                     # LLM-as-judge (when needed)
    GENERAL = "general"                           # default fallback


class Operation(str, Enum):
    MUTATE = "mutate"
    PATCH = "patch"           # targeted diff, not full rewrite
    REVIEW = "review"
    PLAN = "plan"
    ANALYZE = "analyze"
    EVALUATE = "evaluate"


@dataclass
class Budget:
    """Provider-neutral budgets, observed where available."""

    max_tokens: Optional[int] = None
    max_input_tokens: Optional[int] = None
    timeout_s: Optional[float] = None
    # Informational, not enforced by core unless host reports it
    token_budget: Optional[int] = None
    cost_budget: Optional[float] = None


@dataclass
class BrainRequest:
    """
    Describes work for the brain, not a vendor call.

    Fields are intentionally vendor-agnostic. The host (OpenCode) decides
    which provider/model fulfills it based on the user's OpenCode selection
    and capability negotiation.
    """

    operation: Operation = Operation.MUTATE
    objective: str = ""
    # Relevant context packet — compact, not full history
    context: Dict[str, Any] = field(default_factory=dict)
    # Parent/candidate information (patch-first)
    parent_code: Optional[str] = None
    parent_id: Optional[str] = None
    parent_metrics: Optional[Dict[str, float]] = None
    mutation_strategy: Optional[str] = None  # operator key, e.g. LOCAL_OPTIMIZE
    constraints: Optional[Dict[str, Any]] = None
    required_output_schema: Optional[Dict[str, Any]] = None  # JSON schema when needed
    budget: Budget = field(default_factory=Budget)
    # Prompt/policy mode (replaces old role→model routing)
    policy: PolicyMode = PolicyMode.GENERAL
    # Optional session/experiment linkage for observability
    session_id: Optional[str] = None
    candidate_id: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "operation": self.operation.value,
            "objective": self.objective,
            "context": self.context,
            "parent_code": self.parent_code,
            "parent_id": self.parent_id,
            "parent_metrics": self.parent_metrics,
            "mutation_strategy": self.mutation_strategy,
            "constraints": self.constraints,
            "required_output_schema": self.required_output_schema,
            "budget": {
                "max_tokens": self.budget.max_tokens,
                "max_input_tokens": self.budget.max_input_tokens,
                "timeout_s": self.budget.timeout_s,
                "token_budget": self.budget.token_budget,
                "cost_budget": self.budget.cost_budget,
            },
            "policy": self.policy.value,
            "session_id": self.session_id,
            "candidate_id": self.candidate_id,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "BrainRequest":
        b = d.get("budget") or {}
        return cls(
            operation=Operation(d.get("operation", Operation.MUTATE.value)),
            objective=d.get("objective", ""),
            context=dict(d.get("context") or {}),
            parent_code=d.get("parent_code"),
            parent_id=d.get("parent_id"),
            parent_metrics=d.get("parent_metrics"),
            mutation_strategy=d.get("mutation_strategy"),
            constraints=d.get("constraints"),
            required_output_schema=d.get("required_output_schema"),
            budget=Budget(
                max_tokens=b.get("max_tokens"),
                max_input_tokens=b.get("max_input_tokens"),
                timeout_s=b.get("timeout_s"),
                token_budget=b.get("token_budget"),
                cost_budget=b.get("cost_budget"),
            ),
            policy=PolicyMode(d.get("policy", PolicyMode.GENERAL.value)),
            session_id=d.get("session_id"),
            candidate_id=d.get("candidate_id"),
            extra=dict(d.get("extra") or {}),
        )


@dataclass
class BrainResponse:
    """Provider-neutral response."""

    content: str
    # Structured output when requested (parsed JSON dict)
    structured: Optional[Dict[str, Any]] = None
    # Host-reported usage for token/budget tracking
    usage: Dict[str, int] = field(default_factory=dict)
    # Latency and provenance (recorded as metadata, not routing logic)
    latency_ms: Optional[float] = None
    model_meta: Dict[str, Any] = field(default_factory=dict)
    # Reasoning tokens if reported
    reasoning_tokens: Optional[int] = None
    # Whether output was truncated — callers should retry with larger budget
    truncated: bool = False
    # Error details when not ok
    error: Optional[str] = None
    # Whether the brain considers this a successful completion
    ok: bool = True

    @property
    def reasoning_used(self) -> int:
        if self.reasoning_tokens is not None:
            return int(self.reasoning_tokens)
        details = (self.usage or {}).get("completion_tokens_details") or {}
        try:
            return int(details.get("reasoning_tokens") or 0)
        except (TypeError, ValueError):
            return 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "content": self.content,
            "structured": self.structured,
            "usage": self.usage,
            "latency_ms": self.latency_ms,
            "model_meta": self.model_meta,
            "reasoning_tokens": self.reasoning_tokens,
            "truncated": self.truncated,
            "error": self.error,
            "ok": self.ok,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "BrainResponse":
        return cls(
            content=d.get("content", ""),
            structured=d.get("structured"),
            usage=dict(d.get("usage") or {}),
            latency_ms=d.get("latency_ms"),
            model_meta=dict(d.get("model_meta") or {}),
            reasoning_tokens=d.get("reasoning_tokens"),
            truncated=bool(d.get("truncated", False)),
            error=d.get("error"),
            ok=bool(d.get("ok", True)),
        )

    @classmethod
    def failure(cls, error: str, latency_ms: Optional[float] = None) -> "BrainResponse":
        return cls(content="", error=error, ok=False, latency_ms=latency_ms)
