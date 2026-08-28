from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Mapping, Protocol

from control_plane.agent import (
    AgentRunResult,
    ContextItem,
    Goal,
    KnowledgeItem,
    RoutedModelRuntime,
)
from control_plane.providers.profiles import Role

from openevolve.database import Program, ProgramDatabase

ArtifactScalar = str | bytes | int | float | bool | None


class ProgramEvaluator(Protocol):
    async def evaluate_program(
        self, program_code: str, program_id: str = ""
    ) -> dict[str, float]: ...

    def get_pending_artifacts(
        self, program_id: str
    ) -> Mapping[str, ArtifactScalar] | None: ...


@dataclass(frozen=True, slots=True)
class NativeEvolutionSettings:
    output_dir: Path
    initial_code: str
    candidate_filename: str
    language: str
    initial_changes_description: str = "initial program"
    repository_root: Path | None = None
    worktree_root: Path | None = None
    worktree_base_ref: str = "HEAD"


@dataclass(frozen=True, slots=True)
class NativeEvolutionDependencies:
    database: ProgramDatabase
    evaluator: ProgramEvaluator
    model_runtime: RoutedModelRuntime
    settings: NativeEvolutionSettings


@dataclass(frozen=True, slots=True)
class NativeGenerationRequest:
    goal: Goal
    iteration: int
    parent_id: str | None = None
    role: Role = Role.ORCHESTRATOR
    context_items: tuple[ContextItem, ...] = ()
    context_token_budget: int = 8_192
    max_steps: int = 12
    max_tool_calls: int = 32


@dataclass(frozen=True, slots=True)
class NativeVerificationResult:
    accepted: bool
    checks: tuple[str, ...]
    reason: str | None = None


class NativeGenerationStatus(str, Enum):
    EVALUATED = "evaluated"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class NativeGenerationResult:
    candidate_id: str
    parent_id: str
    status: NativeGenerationStatus
    agent_result: AgentRunResult
    verification: NativeVerificationResult
    workspace: str
    program: Program | None = None
    inherited_knowledge: tuple[KnowledgeItem, ...] = ()
    rejection_reason: str | None = None
    metrics: Mapping[str, float] = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return self.status is NativeGenerationStatus.EVALUATED


@dataclass(frozen=True, slots=True)
class ParentProgramNotFoundError(LookupError):
    parent_id: str

    def __str__(self) -> str:
        return f"native evolution parent was not found: {self.parent_id!r}"
