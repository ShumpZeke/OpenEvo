from __future__ import annotations

import json
from typing import Callable

from control_plane.agent import EventLog, NativeAgentRuntime
from control_plane.telemetry.events import Component, Event, EventType, Status

from .native_evolution_support import (
    CandidateEventSpec,
    EvaluationOutcome,
    GenerationOutcome,
    GenerationState,
    NativeEvolutionSupport,
)
from .native_evolution_types import (
    ArtifactScalar,
    NativeGenerationResult,
    NativeGenerationStatus,
    NativeVerificationResult,
)


class NativeEvolutionLifecycle(NativeEvolutionSupport):
    event_log: EventLog

    async def _evaluate(
        self,
        code: str,
        candidate_id: str,
        runtime: NativeAgentRuntime | None = None,
    ) -> dict[str, float]:
        record: Callable[[Event], None] = (
            runtime.kernel.record if runtime is not None else self.event_log.append
        )
        record(
            Event(
                EventType.EVALUATOR_STARTED,
                Component.EVALUATOR,
                trace_id=candidate_id,
                candidate_id=candidate_id,
                status=Status.RUNNING,
                summary="independent evaluator started",
            )
        )
        metrics = await self.dependencies.evaluator.evaluate_program(code, candidate_id)
        record(
            Event(
                EventType.EVALUATOR_COMPLETED,
                Component.EVALUATOR,
                trace_id=candidate_id,
                candidate_id=candidate_id,
                summary="independent evaluator completed",
                metrics={key: float(value) for key, value in metrics.items()},
            )
        )
        return metrics

    def _reject_before_evaluation(
        self, state: GenerationState, verdict: NativeVerificationResult
    ) -> NativeGenerationResult:
        return self._rejected_result(
            state, verdict, EvaluationOutcome(failure=verdict.reason)
        )

    def _reject_after_evaluation(
        self,
        state: GenerationState,
        verdict: NativeVerificationResult,
        evaluation: EvaluationOutcome,
    ) -> NativeGenerationResult:
        self._persist_evaluator_artifacts(state)
        return self._rejected_result(state, verdict, evaluation)

    def _rejected_result(
        self,
        state: GenerationState,
        verdict: NativeVerificationResult,
        evaluation: EvaluationOutcome,
    ) -> NativeGenerationResult:
        identity = state.identity
        reason = evaluation.failure or verdict.reason or "candidate rejected"
        trajectory = self._trajectory(
            state,
            GenerationOutcome(NativeGenerationStatus.REJECTED, verdict, evaluation),
        )
        state.runtime.world.write(".openevo/trajectory.json", trajectory)
        self._remember_failure(identity.selection.parent.id, reason)
        self._save_goal(
            state.runtime, identity.request.goal, f"rejected:{identity.candidate_id}"
        )
        state.runtime.kernel.record(
            self._candidate_event(
                identity,
                CandidateEventSpec(
                    EventType.CANDIDATE_REJECTED,
                    Status.REJECTED,
                    evaluation.metrics,
                ),
            )
        )
        return NativeGenerationResult(
            candidate_id=identity.candidate_id,
            parent_id=identity.selection.parent.id,
            status=NativeGenerationStatus.REJECTED,
            agent_result=state.agent_result,
            verification=verdict,
            workspace=str(state.runtime.world.root),
            inherited_knowledge=identity.selection.inherited,
            rejection_reason=reason,
            metrics=evaluation.metrics,
        )

    def _persist_evaluator_artifacts(self, state: GenerationState) -> None:
        artifacts = self._pending_artifacts(state.identity.candidate_id)
        if not artifacts:
            return
        state.runtime.world.write(
            ".openevo/evaluator_artifacts.json",
            json.dumps(artifacts, sort_keys=True, separators=(",", ":")),
        )

    def _pending_artifacts(self, candidate_id: str) -> dict[str, str]:
        artifacts = self.dependencies.evaluator.get_pending_artifacts(candidate_id)
        if not artifacts:
            return {}
        return {
            key: self.redactor.redact_text(_artifact_text(value))
            for key, value in artifacts.items()
        }


def _artifact_text(value: ArtifactScalar) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
