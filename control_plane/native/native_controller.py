from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from control_plane.agent import (
    AgentKernel,
    AgentRunResult,
    AgentSessionState,
    AgentSessionStore,
    AgentTask,
    ContextItem,
    EventLog,
    ExecutionWorld,
    Goal,
    GoalStore,
    GitExecutableNotFoundError,
    LineageMemory,
    ModelToolDefinition,
    NativeAgentRuntime,
    NativeCandidate,
    OpenAICompatibleProvider,
    RoutedModelRuntime,
    discover_git_repository,
    native_world_tool_definitions,
    native_world_tools,
)
from control_plane.providers.profiles import Role
from control_plane.providers.router import ModelRouter

from openevolve.database import ProgramDatabase
from openevolve.evaluator import Evaluator
from .native_evolution import (
    NativeEvolutionCoordinator,
    NativeEvolutionDependencies,
    NativeEvolutionSettings,
    NativeGenerationRequest,
    NativeGenerationResult,
)


@dataclass(frozen=True, slots=True)
class NativeGenerationContext:
    initial_program_path: Path
    initial_program_code: str
    language: str
    initial_changes_description: str
    database: ProgramDatabase
    evaluator: Evaluator


class NativeController:
    def __init__(self, output_dir: Path) -> None:
        self._output_dir = output_dir

    def create_runtime(self, world_name: str = "candidate") -> NativeAgentRuntime:
        output_dir = self._output_dir
        event_log = EventLog(str(output_dir / "agent_events.ndjson"))
        return NativeAgentRuntime(
            ExecutionWorld(str(output_dir / "agent_worlds" / world_name)),
            kernel=AgentKernel(event_sink=event_log.append),
            memory=LineageMemory(str(output_dir / "agent_memory.ndjson")),
            goals=GoalStore(str(output_dir / "agent_goals.ndjson")),
            sessions=AgentSessionStore(
                str(output_dir / "agent_sessions"),
                event_sink=event_log.append,
            ),
        )

    def run_agent(
        self,
        goal: Goal,
        tasks: Sequence[AgentTask],
        runner: Callable[[AgentTask], str],
        evaluator: Callable[[ExecutionWorld], Mapping[str, float]],
        acceptance: Callable[[Mapping[str, float]], bool],
        world_name: str = "candidate",
        max_workers: int = 4,
    ) -> NativeCandidate:
        runtime = self.create_runtime(world_name)
        return runtime.run(goal, tasks, runner, evaluator, acceptance, max_workers)

    def run_model_agent(
        self,
        goal: Goal,
        model_runtime: RoutedModelRuntime | None = None,
        tools: Sequence[ModelToolDefinition] | None = None,
        context_items: Sequence[ContextItem] = (),
        role: Role = Role.ORCHESTRATOR,
        world_name: str = "candidate",
        max_steps: int = 12,
        max_tool_calls: int = 32,
        session_id: str | None = None,
    ) -> AgentRunResult:
        runtime = self.create_runtime(world_name)
        for tool in native_world_tools(runtime.world):
            runtime.register_tool(tool)
        routed = model_runtime or RoutedModelRuntime(
            ModelRouter(),
            OpenAICompatibleProvider(),
            event_sink=runtime.kernel.record,
        )
        return runtime.run_model_goal(
            goal,
            routed,
            tuple(tools) if tools is not None else native_world_tool_definitions(),
            role=role,
            context_items=tuple(context_items),
            max_steps=max_steps,
            max_tool_calls=max_tool_calls,
            session_id=session_id,
        )

    def fork_model_session(self, session_id: str) -> AgentSessionState:
        output_dir = self._output_dir
        event_log = EventLog(str(output_dir / "agent_events.ndjson"))
        return AgentSessionStore(
            str(output_dir / "agent_sessions"),
            event_sink=event_log.append,
        ).fork(session_id)

    async def run_generation(
        self,
        context: NativeGenerationContext,
        request: NativeGenerationRequest,
        model_runtime: RoutedModelRuntime | None = None,
    ) -> NativeGenerationResult:
        routed = model_runtime or RoutedModelRuntime(
            ModelRouter(), OpenAICompatibleProvider()
        )
        source_path = context.initial_program_path.resolve()
        try:
            repository_root = discover_git_repository(source_path.parent)
        except GitExecutableNotFoundError:
            repository_root = None
        candidate_filename = (
            source_path.relative_to(repository_root).as_posix()
            if repository_root is not None
            else source_path.name
        )
        coordinator = NativeEvolutionCoordinator(
            NativeEvolutionDependencies(
                database=context.database,
                evaluator=context.evaluator,
                model_runtime=routed,
                settings=NativeEvolutionSettings(
                    output_dir=self._output_dir,
                    initial_code=context.initial_program_code,
                    candidate_filename=candidate_filename,
                    language=context.language,
                    initial_changes_description=context.initial_changes_description,
                    repository_root=repository_root,
                ),
            )
        )
        return await coordinator.run_generation(request)
