from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from threading import Lock
from typing import Callable, Dict, List, Mapping, Optional, Protocol, Sequence

from ..telemetry.events import Component, Event, EventType, Status, new_id
from .delegation import NoEligibleRoleError, TaskDelegator


class TaskStatus(str, Enum):
    QUEUED = "queued"
    READY = "ready"
    RUNNING = "running"
    BLOCKED = "blocked"
    FAILED = "failed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class Goal:
    objective: str
    success_conditions: tuple[str, ...] = ()
    goal_id: str = field(default_factory=lambda: new_id("goal_"))


@dataclass(frozen=True, slots=True)
class ToolCall:
    name: str
    arguments: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolResult:
    tool: str
    succeeded: bool
    output: str = ""
    error: Optional[str] = None
    duration_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class ToolRegistrationError(ValueError):
    tool_name: str

    def __str__(self) -> str:
        return f"tool name is unavailable: {self.tool_name!r}"


@dataclass(frozen=True, slots=True)
class DuplicateTaskIdError(ValueError):
    task_ids: tuple[str, ...]

    def __str__(self) -> str:
        return "task ids must be unique"


class Tool(Protocol):
    name: str

    def invoke(self, call: ToolCall) -> ToolResult: ...


@dataclass(slots=True)  # noqa: MUTABLE_OK
class AgentTask:
    """Mutable lifecycle record owned by one AgentKernel run."""

    objective: str
    dependencies: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    budget: int = 1
    task_id: str = field(default_factory=lambda: new_id("task_"))
    status: TaskStatus = TaskStatus.QUEUED
    assigned_agent: Optional[str] = None
    output: Optional[str] = None
    error: Optional[str] = None


class EventSink(Protocol):
    def __call__(self, event: Event) -> None: ...


TaskRunner = Callable[[AgentTask], str]


class AgentKernel:
    def __init__(
        self,
        event_sink: Optional[EventSink] = None,
        delegator: Optional[TaskDelegator] = None,
    ) -> None:
        self._tools: Dict[str, Tool] = {}
        self._events: List[Event] = []
        self._lock = Lock()
        self._event_sink = event_sink
        self._delegator = delegator

    def register_tool(self, tool: Tool) -> None:
        if not tool.name or tool.name in self._tools:
            raise ToolRegistrationError(tool.name)
        self._tools[tool.name] = tool

    def tool(self, call: ToolCall) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(call.name, False, error="tool is not registered")
        started = time.perf_counter()
        try:
            result = tool.invoke(call)
        # Tools are an extension boundary; one broken tool must not crash the agent loop.
        except Exception as exc:  # noqa: BROAD_EXCEPT_OK
            result = ToolResult(call.name, False, error=type(exc).__name__)
        elapsed = (time.perf_counter() - started) * 1000
        return ToolResult(
            result.tool, result.succeeded, result.output, result.error, elapsed
        )

    def run(
        self,
        goal: Goal,
        tasks: Sequence[AgentTask],
        runner: TaskRunner,
        max_workers: int = 4,
    ) -> tuple[AgentTask, ...]:
        task_map = {task.task_id: task for task in tasks}
        if len(task_map) != len(tasks):
            raise DuplicateTaskIdError(tuple(task.task_id for task in tasks))
        self._emit(
            Event(
                EventType.CONTROL_COMMAND,
                Component.CONTROL_PLANE,
                trace_id=goal.goal_id,
                summary="goal started",
                metadata={"objective": goal.objective},
            )
        )
        remaining = set(task_map)
        while remaining:
            ready = [
                task_map[task_id]
                for task_id in remaining
                if all(
                    dep in task_map and task_map[dep].status is TaskStatus.COMPLETED
                    for dep in task_map[task_id].dependencies
                )
            ]
            if not ready:
                for task_id in remaining:
                    task_map[task_id].status = TaskStatus.BLOCKED
                break
            runnable = []
            for task in ready:
                if not self._assign_role(goal, task):
                    remaining.remove(task.task_id)
                    continue
                task.status = TaskStatus.READY
                self._emit(
                    Event(
                        EventType.AGENT_TASK_STARTED,
                        Component.CONTROL_PLANE,
                        trace_id=goal.goal_id,
                        summary=task.objective,
                        metadata={
                            "task_id": task.task_id,
                            "assigned_agent": task.assigned_agent,
                        },
                    )
                )
                runnable.append(task)
            if not runnable:
                continue
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {
                    pool.submit(self._run_task, task, runner): task for task in runnable
                }
                for future, task in futures.items():
                    try:
                        task.output = future.result()
                        task.status = TaskStatus.COMPLETED
                    # Runners are model-supplied; isolate a failed DAG node from its peers.
                    except Exception as exc:  # noqa: BROAD_EXCEPT_OK
                        task.status = TaskStatus.FAILED
                        task.error = type(exc).__name__
                        self._emit(
                            Event(
                                EventType.AGENT_TASK_FAILED,
                                Component.CONTROL_PLANE,
                                trace_id=goal.goal_id,
                                status=Status.FAILED,
                                summary=task.objective,
                                metadata={"task_id": task.task_id, "error": task.error},
                            )
                        )
                        remaining.remove(task.task_id)
                        continue
                    self._emit(
                        Event(
                            EventType.AGENT_TASK_COMPLETED,
                            Component.CONTROL_PLANE,
                            trace_id=goal.goal_id,
                            summary=task.objective,
                            metadata={"task_id": task.task_id},
                        )
                    )
                    remaining.remove(task.task_id)
        self._emit(
            Event(
                EventType.CONTROL_COMMAND,
                Component.CONTROL_PLANE,
                trace_id=goal.goal_id,
                status=Status.OK,
                summary="goal scheduling finished",
                metadata={"remaining": len(remaining)},
            )
        )
        return tuple(tasks)

    def events(self) -> tuple[Event, ...]:
        with self._lock:
            return tuple(self._events)

    def record(self, event: Event) -> None:
        self._emit(event)

    def _run_task(self, task: AgentTask, runner: TaskRunner) -> str:
        task.status = TaskStatus.RUNNING
        return runner(task)

    def _assign_role(self, goal: Goal, task: AgentTask) -> bool:
        if self._delegator is None or task.assigned_agent is not None:
            return True
        try:
            assignment = self._delegator.assign(task)
        except NoEligibleRoleError as exc:
            task.status = TaskStatus.BLOCKED
            task.error = type(exc).__name__
            self._emit(
                Event(
                    EventType.AGENT_TASK_BLOCKED,
                    Component.CONTROL_PLANE,
                    trace_id=goal.goal_id,
                    status=Status.REJECTED,
                    summary=task.objective,
                    metadata={
                        "task_id": task.task_id,
                        "required_capabilities": task.required_capabilities,
                    },
                )
            )
            return False
        task.assigned_agent = assignment.role
        return True

    def _emit(self, event: Event) -> None:
        with self._lock:
            self._events.append(event)
        if self._event_sink is not None:
            self._event_sink(event)
