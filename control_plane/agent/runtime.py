from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Sequence

from ..providers.profiles import Role
from ..telemetry.events import Component, Event, EventType, Status, new_id
from .context import ContextItem
from .kernel import AgentKernel, AgentTask, Goal, TaskRunner, Tool
from .loop import NativeAgentLoop
from .run_types import AgentRunResult
from .model import ModelToolDefinition, RoutedModelRuntime
from .memory import KnowledgeItem, LineageMemory
from .goals import GoalStore
from .codeintel import PythonCodeIndex
from .experiments import ExperimentEngine
from .world import ExecutionWorld
from .session import AgentSessionStore


@dataclass(frozen=True, slots=True)
class NativeCandidate:
    candidate_id: str
    goal_id: str
    workspace: str
    task_ids: tuple[str, ...]
    metrics: Mapping[str, float] = field(default_factory=dict)
    evidence: tuple[str, ...] = ()
    accepted: bool = False
    inherited_knowledge: tuple[KnowledgeItem, ...] = ()


Evaluator = Callable[[ExecutionWorld], Mapping[str, float]]


class NativeAgentRuntime:
    def __init__(
        self,
        world: ExecutionWorld,
        kernel: Optional[AgentKernel] = None,
        memory: Optional[LineageMemory] = None,
        goals: Optional[GoalStore] = None,
        sessions: AgentSessionStore | None = None,
    ) -> None:
        self.world = world
        self.kernel = kernel or AgentKernel()
        self.memory = memory
        self.goals = goals
        self.sessions = sessions
        self.code_index = PythonCodeIndex(str(world.root))
        self.experiments = ExperimentEngine()

    @property
    def events(self) -> tuple[Event, ...]:
        return self.kernel.events()

    def register_tool(self, tool: Tool) -> None:
        self.kernel.register_tool(tool)

    def run_model_goal(
        self,
        goal: Goal,
        model_runtime: RoutedModelRuntime,
        tools: tuple[ModelToolDefinition, ...],
        role: Role = Role.ORCHESTRATOR,
        context_items: tuple[ContextItem, ...] = (),
        context_token_budget: int = 8_192,
        max_steps: int = 12,
        max_tool_calls: int = 32,
        session_id: str | None = None,
    ) -> AgentRunResult:
        if model_runtime.event_sink is None:
            model_runtime.event_sink = self.kernel.record
        return NativeAgentLoop(
            self.kernel,
            model_runtime,
            sessions=self.sessions,
            session_id=session_id,
        ).run(
            goal,
            tools,
            role=role,
            context_items=context_items,
            context_token_budget=context_token_budget,
            max_steps=max_steps,
            max_tool_calls=max_tool_calls,
        )

    def run(
        self,
        goal: Goal,
        tasks: Sequence[AgentTask],
        runner: TaskRunner,
        evaluator: Evaluator,
        acceptance: Callable[[Mapping[str, float]], bool],
        max_workers: int = 4,
        parent_ids: Sequence[str] = (),
    ) -> NativeCandidate:
        inherited = self.memory.inherit(parent_ids) if self.memory else ()
        if self.goals and self.goals.get(goal.goal_id) is None:
            self.goals.create(goal, (task.task_id for task in tasks))
        completed = self.kernel.run(goal, tasks, runner, max_workers=max_workers)
        failed = tuple(task for task in completed if task.status.value != "completed")
        if failed:
            return NativeCandidate(
                new_id("candidate_"),
                goal.goal_id,
                str(self.world.root),
                tuple(task.task_id for task in completed),
                evidence=("task_failure",),
                inherited_knowledge=inherited,
            )
        candidate_id = new_id("candidate_")
        metrics = dict(evaluator(self.world))
        accepted = acceptance(metrics)
        evidence = ("tasks_completed", "independent_evaluator")
        self.kernel._emit(
            Event(
                EventType.CANDIDATE_CREATED,
                Component.ENGINE,
                trace_id=goal.goal_id,
                candidate_id=candidate_id,
                status=Status.OK,
                summary="native candidate evaluated",
                metrics={key: float(value) for key, value in metrics.items()},
                metadata={"accepted": accepted, "workspace": str(self.world.root)},
            )
        )
        return NativeCandidate(
            candidate_id,
            goal.goal_id,
            str(self.world.root),
            tuple(task.task_id for task in completed),
            metrics,
            evidence,
            accepted,
            inherited,
        )
