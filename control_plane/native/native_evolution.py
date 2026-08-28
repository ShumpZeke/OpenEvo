from __future__ import annotations

import os
from functools import partial

import anyio

from control_plane.agent import (
    AgentRunResult,
    AgentSessionStore,
    EventLog,
    GoalStore,
    LineageMemory,
    NativeAgentRuntime,
    RoutedModelRuntime,
    native_world_tool_definitions,
)
from control_plane.telemetry.events import EventType, Status, new_id
from control_plane.telemetry.redaction import Redactor

from openevolve.database import Program
from .native_evolution_types import (
    NativeEvolutionDependencies,
    NativeEvolutionSettings,
    NativeGenerationRequest,
    NativeGenerationResult,
    NativeGenerationStatus,
    ParentProgramNotFoundError,
)
from .native_evolution_support import (
    CandidateEventSpec,
    CandidateIdentity,
    EvaluationOutcome,
    GenerationOutcome,
    GenerationState,
    ParentSelection,
    evaluation_failure,
)
from .native_evolution_lifecycle import NativeEvolutionLifecycle
from .native_world_factory import NativeWorldFactory, NativeWorldServices

__all__ = [
    "NativeEvolutionCoordinator",
    "NativeEvolutionDependencies",
    "NativeEvolutionSettings",
    "NativeGenerationRequest",
    "NativeGenerationResult",
    "NativeGenerationStatus",
    "ParentProgramNotFoundError",
]


class NativeEvolutionCoordinator(NativeEvolutionLifecycle):
    def __init__(self, dependencies: NativeEvolutionDependencies) -> None:
        self.dependencies = dependencies
        settings = dependencies.settings
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        self.memory = LineageMemory(str(settings.output_dir / "agent_memory.ndjson"))
        self.goals = GoalStore(str(settings.output_dir / "agent_goals.ndjson"))
        self.redactor = Redactor()
        for profile in dependencies.model_runtime.router.profiles.values():
            if profile.secret_ref:
                self.redactor.register_value(os.environ.get(profile.secret_ref))
        self.event_log = EventLog(
            str(settings.output_dir / "agent_events.ndjson"), self.redactor
        )
        sessions = AgentSessionStore(
            str(settings.output_dir / "agent_sessions"),
            self.redactor,
            self.event_log.append,
        )
        self.worlds = NativeWorldFactory(
            NativeWorldServices(
                settings,
                self.memory,
                self.goals,
                sessions,
                self.event_log.append,
            )
        )

    async def ensure_initial_program(self, evaluate: bool = True) -> Program:
        existing = next(iter(self.dependencies.database.programs.values()), None)
        if existing is not None:
            return existing
        candidate_id = new_id("program_")
        metrics = (
            await self._evaluate(self.dependencies.settings.initial_code, candidate_id)
            if evaluate
            else {"combined_score": 0.0}
        )
        settings = self.dependencies.settings
        program = Program(
            id=candidate_id,
            code=settings.initial_code,
            changes_description=settings.initial_changes_description,
            language=settings.language,
            metrics=metrics,
            iteration_found=0,
            metadata={"native_seed": True},
        )
        self.dependencies.database.add(program, iteration=0)
        artifacts = self._pending_artifacts(candidate_id)
        if artifacts:
            stored_artifacts: dict[str, str | bytes] = dict(artifacts)
            self.dependencies.database.store_artifacts(candidate_id, stored_artifacts)
        return program

    async def run_generation(
        self, request: NativeGenerationRequest
    ) -> NativeGenerationResult:
        await self.ensure_initial_program()
        selection = self._select_parent(request)
        candidate_id = new_id("program_")
        identity = CandidateIdentity(request, selection, candidate_id)
        runtime = self._create_runtime(candidate_id)
        runtime.world.write(
            self.dependencies.settings.candidate_filename, selection.parent.code
        )
        self._save_goal(runtime, request.goal, f"realizing:{candidate_id}")
        runtime.kernel.record(
            self._candidate_event(
                identity, CandidateEventSpec(EventType.CANDIDATE_SAMPLED)
            )
        )
        runtime.kernel.record(
            self._candidate_event(
                identity,
                CandidateEventSpec(
                    EventType.CANDIDATE_REALIZATION_STARTED, Status.RUNNING
                ),
            )
        )
        agent_result = await self._run_agent(runtime, identity)
        state = GenerationState(identity, runtime, agent_result)
        runtime.kernel.record(
            self._candidate_event(
                identity, CandidateEventSpec(EventType.CANDIDATE_REALIZATION_COMPLETED)
            )
        )
        verified = self._verify(state)
        if not verified.verdict.accepted or verified.code is None:
            return self._reject_before_evaluation(state, verified.verdict)

        runtime.kernel.record(
            self._candidate_event(
                identity,
                CandidateEventSpec(
                    EventType.CANDIDATE_EVALUATION_STARTED, Status.RUNNING
                ),
            )
        )
        metrics = await self._evaluate(verified.code, candidate_id, runtime)
        evaluation = EvaluationOutcome(metrics, evaluation_failure(metrics))
        if evaluation.failure is not None:
            return self._reject_after_evaluation(state, verified.verdict, evaluation)

        program = self._create_program(state, verified.code, evaluation)
        self.dependencies.database.add(program, iteration=request.iteration)
        trajectory = self._trajectory(
            state,
            GenerationOutcome(
                NativeGenerationStatus.EVALUATED, verified.verdict, evaluation
            ),
        )
        runtime.world.write(".openevo/trajectory.json", trajectory)
        artifacts = self._pending_artifacts(candidate_id)
        artifacts["native_trajectory.json"] = trajectory
        stored_artifacts = dict[str, str | bytes](artifacts)
        self.dependencies.database.store_artifacts(candidate_id, stored_artifacts)
        self._remember_success(program, agent_result)
        self._save_goal(runtime, request.goal, f"evaluated:{candidate_id}")
        event_types = [
            EventType.CANDIDATE_EVALUATION_COMPLETED,
            EventType.CANDIDATE_CREATED,
            EventType.POPULATION_UPDATED,
        ]
        if candidate_id in self.dependencies.database.archive:
            event_types.append(EventType.CANDIDATE_PROMOTED)
        for event_type in event_types:
            runtime.kernel.record(
                self._candidate_event(
                    identity, CandidateEventSpec(event_type, metrics=metrics)
                )
            )
        return NativeGenerationResult(
            candidate_id,
            selection.parent.id,
            NativeGenerationStatus.EVALUATED,
            agent_result,
            verified.verdict,
            str(runtime.world.root),
            program,
            selection.inherited,
            metrics=metrics,
        )

    def _select_parent(self, request: NativeGenerationRequest) -> ParentSelection:
        database = self.dependencies.database
        if request.parent_id is not None:
            parent = database.get(request.parent_id)
            if parent is None:
                raise ParentProgramNotFoundError(request.parent_id)
            inspirations: tuple[Program, ...] = ()
        else:
            parent, sampled = database.sample()
            inspirations = tuple(sampled)
        return ParentSelection(parent, inspirations, self.memory.inherit((parent.id,)))

    def _create_runtime(self, candidate_id: str) -> NativeAgentRuntime:
        return self.worlds.create(candidate_id)

    async def _run_agent(
        self,
        runtime: NativeAgentRuntime,
        identity: CandidateIdentity,
    ) -> AgentRunResult:
        request = identity.request
        routed = RoutedModelRuntime(
            self.dependencies.model_runtime.router,
            self.dependencies.model_runtime.provider,
            runtime.kernel.record,
        )
        call = partial(
            runtime.run_model_goal,
            request.goal,
            routed,
            native_world_tool_definitions(),
            role=request.role,
            context_items=self._context(identity),
            context_token_budget=request.context_token_budget,
            max_steps=request.max_steps,
            max_tool_calls=request.max_tool_calls,
        )
        return await anyio.to_thread.run_sync(call)
