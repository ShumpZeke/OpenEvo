from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping

from control_plane.agent import (
    AgentRunResult,
    AgentRunStatus,
    ContextItem,
    Goal,
    KnowledgeItem,
    LineageMemory,
    NativeAgentRuntime,
)
from control_plane.telemetry.events import Component, Event, EventType, Status
from control_plane.telemetry.redaction import Redactor

from openevolve.database import Program
from .native_evolution_types import (
    NativeEvolutionDependencies,
    NativeGenerationRequest,
    NativeGenerationResult,
    NativeGenerationStatus,
    NativeVerificationResult,
)
from .native_trajectory import (
    NativeTrajectoryRecord,
    serialize_native_trajectory,
)


@dataclass(frozen=True, slots=True)
class ParentSelection:
    parent: Program
    inspirations: tuple[Program, ...]
    inherited: tuple[KnowledgeItem, ...]


@dataclass(frozen=True, slots=True)
class CandidateIdentity:
    request: NativeGenerationRequest
    selection: ParentSelection
    candidate_id: str


@dataclass(frozen=True, slots=True)
class GenerationState:
    identity: CandidateIdentity
    runtime: NativeAgentRuntime
    agent_result: AgentRunResult


@dataclass(frozen=True, slots=True)
class EvaluationOutcome:
    metrics: Mapping[str, float] = field(default_factory=dict)
    failure: str | None = None


@dataclass(frozen=True, slots=True)
class GenerationOutcome:
    status: NativeGenerationStatus
    verdict: NativeVerificationResult
    evaluation: EvaluationOutcome


@dataclass(frozen=True, slots=True)
class CandidateEventSpec:
    event_type: EventType
    status: Status = Status.OK
    metrics: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VerifiedSource:
    verdict: NativeVerificationResult
    code: str | None = None


class NativeEvolutionSupport:
    dependencies: NativeEvolutionDependencies
    memory: LineageMemory
    redactor: Redactor

    def _context(self, identity: CandidateIdentity) -> tuple[ContextItem, ...]:
        settings = self.dependencies.settings
        selection = identity.selection
        items = list(identity.request.context_items)
        items.append(
            ContextItem(
                "runtime",
                f"candidate-contract-{identity.candidate_id}",
                f"The parent is seeded at {settings.candidate_filename}. Modify and "
                "verify that file; only its final content is evaluated.",
                1.0,
            )
        )
        for index, item in enumerate(selection.inherited):
            items.append(
                ContextItem(
                    "lineage",
                    f"lineage-{index}-{item.source_id}",
                    f"{item.kind}: {item.statement}",
                    0.95,
                )
            )
        for inspiration in selection.inspirations:
            items.append(
                ContextItem(
                    "archive",
                    f"inspiration-{inspiration.id}",
                    inspiration.code,
                    0.55,
                )
            )
        return tuple(items)

    def _verify(self, state: GenerationState) -> VerifiedSource:
        result = state.agent_result
        if result.status is not AgentRunStatus.COMPLETED:
            return VerifiedSource(
                NativeVerificationResult(
                    False, (), f"agent ended with {result.status.value}"
                )
            )
        checks = ["agent_completed"]
        target = state.runtime.world.path(self.dependencies.settings.candidate_filename)
        if not target.is_file():
            return VerifiedSource(
                NativeVerificationResult(
                    False, tuple(checks), "candidate file is missing"
                )
            )
        checks.append("target_exists")
        code = target.read_text(encoding="utf-8")
        if not code.strip():
            return VerifiedSource(
                NativeVerificationResult(
                    False, tuple(checks), "candidate code is empty"
                )
            )
        checks.append("candidate_nonempty")
        if code == state.identity.selection.parent.code:
            return VerifiedSource(
                NativeVerificationResult(
                    False, tuple(checks), "candidate code is unchanged"
                )
            )
        checks.append("candidate_changed")
        return VerifiedSource(NativeVerificationResult(True, tuple(checks)), code)

    def _candidate_event(
        self, identity: CandidateIdentity, spec: CandidateEventSpec
    ) -> Event:
        parent = identity.selection.parent
        island = parent.metadata.get("island")
        island_id = (
            island if isinstance(island, int) and not isinstance(island, bool) else None
        )
        return Event(
            spec.event_type,
            Component.ENGINE,
            trace_id=identity.request.goal.goal_id,
            generation=parent.generation + 1,
            iteration=identity.request.iteration,
            candidate_id=identity.candidate_id,
            parent_candidate_ids=[parent.id],
            island_id=island_id,
            status=spec.status,
            summary=spec.event_type.value,
            metrics={key: float(value) for key, value in spec.metrics.items()},
        )

    def _create_program(
        self, state: GenerationState, code: str, evaluation: EvaluationOutcome
    ) -> Program:
        identity = state.identity
        result = state.agent_result
        return Program(
            id=identity.candidate_id,
            code=code,
            changes_description=self.redactor.redact_text(result.output)[:2_000],
            language=self.dependencies.settings.language,
            parent_id=identity.selection.parent.id,
            generation=identity.selection.parent.generation + 1,
            iteration_found=identity.request.iteration,
            metrics=dict(evaluation.metrics),
            metadata={
                "native_agent": {
                    "goal_id": identity.request.goal.goal_id,
                    "workspace": str(state.runtime.world.root),
                    "steps": result.steps,
                    "tool_calls": len(result.tool_results),
                    "profiles": list(result.profile_ids),
                    "context_keys": list(result.context_keys),
                    "verification": list(self._verify(state).verdict.checks),
                }
            },
        )

    def _trajectory(
        self,
        state: GenerationState,
        outcome: GenerationOutcome,
    ) -> str:
        identity = state.identity
        return serialize_native_trajectory(
            NativeTrajectoryRecord(
                identity.candidate_id,
                identity.selection.parent.id,
                identity.request.goal.goal_id,
                outcome.status.value,
                state.agent_result,
                outcome.verdict.accepted,
                outcome.verdict.checks,
                outcome.evaluation.failure or outcome.verdict.reason,
                outcome.evaluation.metrics,
            ),
            self.redactor,
        )

    def _save_goal(
        self, runtime: NativeAgentRuntime, goal: Goal, current_state: str
    ) -> None:
        if runtime.goals is None:
            return
        state = runtime.goals.get(goal.goal_id) or runtime.goals.create(goal)
        state.status = "active"
        state.current_state = current_state
        state.evidence = tuple(dict.fromkeys((*state.evidence, current_state)))
        runtime.goals.save(state)

    def _remember_success(self, program: Program, result: AgentRunResult) -> None:
        metrics = ", ".join(
            f"{key}={float(value):.6g}"
            for key, value in sorted(program.metrics.items())
        )
        statement = self.redactor.redact_text(
            f"{result.output.strip()} Metrics: {metrics}"
        )[:2_000]
        score = program.metrics.get("combined_score")
        self.memory.remember(KnowledgeItem("outcome", statement, program.id, score))

    def _remember_failure(self, parent_id: str, reason: str) -> None:
        self.memory.remember(
            KnowledgeItem("failure", self.redactor.redact_text(reason), parent_id)
        )


def evaluation_failure(metrics: Mapping[str, float]) -> str | None:
    if not metrics:
        return "evaluator returned no metrics"
    if "error" in metrics:
        return "evaluator returned error metrics"
    if bool(metrics.get("timeout", 0.0)):
        return "evaluator timed out"
    if any(not math.isfinite(float(value)) for value in metrics.values()):
        return "evaluator returned a non-finite metric"
    return None
