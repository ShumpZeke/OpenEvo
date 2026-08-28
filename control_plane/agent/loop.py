from __future__ import annotations

from dataclasses import dataclass

from ..providers.profiles import Role
from ..telemetry.events import Component, Event, EventType, Status, new_id
from .context import ContextEngine, ContextItem
from .kernel import AgentKernel, Goal, ToolResult
from .model import (
    ConversationMessage,
    MessageRole,
    ModelRequest,
    ModelToolDefinition,
    RoutedModelRuntime,
)
from .prompts import goal_prompt, system_prompt
from .run_types import AgentRunResult


from .session import (
    AgentRunStatus,
    AgentSessionStore,
    SessionProgress,
    SessionStart,
)
from .session_runtime import AgentSessionCoordinator
from .tool_execution import AgentToolExecutor


@dataclass(frozen=True, slots=True)
class InvalidAgentBudgetError(ValueError):
    max_steps: int
    max_tool_calls: int

    def __str__(self) -> str:
        return (
            "agent budgets must be positive: "
            f"max_steps={self.max_steps}, max_tool_calls={self.max_tool_calls}"
        )


@dataclass(frozen=True, slots=True)
class EmptyModelResponseError(RuntimeError):
    profile_id: str

    def __str__(self) -> str:
        return f"model route {self.profile_id!r} returned no content or tool calls"


class NativeAgentLoop:
    def __init__(
        self,
        kernel: AgentKernel,
        model_runtime: RoutedModelRuntime,
        context_engine: ContextEngine | None = None,
        max_tool_output_chars: int = 12_000,
        sessions: AgentSessionStore | None = None,
        session_id: str | None = None,
    ) -> None:
        self.kernel = kernel
        self.model_runtime = model_runtime
        self.context_engine = context_engine or ContextEngine()
        self.session = AgentSessionCoordinator(sessions, session_id)
        self.tool_executor = AgentToolExecutor(kernel, max_tool_output_chars)

    def run(
        self,
        goal: Goal,
        tools: tuple[ModelToolDefinition, ...],
        role: Role = Role.ORCHESTRATOR,
        context_items: tuple[ContextItem, ...] = (),
        context_token_budget: int = 8_192,
        max_steps: int = 12,
        max_tool_calls: int = 32,
    ) -> AgentRunResult:
        if max_steps <= 0 or max_tool_calls <= 0:
            raise InvalidAgentBudgetError(max_steps, max_tool_calls)
        context = self.context_engine.select(
            goal.objective,
            context_items,
            context_token_budget,
        )
        initial_messages = [
            ConversationMessage(MessageRole.SYSTEM, system_prompt()),
            ConversationMessage(
                MessageRole.USER,
                goal_prompt(goal, context.render()),
            ),
        ]
        run_id = new_id("agent_run_")
        session = self.session.start(
            SessionStart(
                goal,
                role,
                tuple(item.key for item in context.items),
                tuple(initial_messages),
                max_steps,
                max_tool_calls,
                run_id,
            )
        )
        messages = list(session.messages) if session is not None else initial_messages
        tool_results = list(session.tool_results) if session is not None else []
        profile_ids = list(session.profile_ids) if session is not None else []
        run_id = session.run_id if session is not None else run_id
        starting_step = session.steps if session is not None else 0
        self.kernel.record(
            Event(
                EventType.AGENT_RUN_STARTED,
                Component.CONTROL_PLANE,
                trace_id=goal.goal_id,
                run_id=run_id,
                status=Status.RUNNING,
                summary=goal.objective,
                metadata={
                    "role": role.value,
                    "max_steps": max_steps,
                    "max_tool_calls": max_tool_calls,
                    "context_keys": list(
                        session.context_keys
                        if session
                        else (item.key for item in context.items)
                    ),
                    "session_id": str(session.session_id) if session else None,
                    "resumed": self.session.session_id is not None,
                },
            )
        )
        for step in range(starting_step + 1, max_steps + 1):
            response = self.model_runtime.complete(
                role,
                ModelRequest(tuple(messages), tools),
                trace_id=goal.goal_id,
            )
            profile_ids.append(response.profile_id)
            messages.append(
                ConversationMessage(
                    MessageRole.ASSISTANT,
                    response.content,
                    tool_calls=response.tool_calls,
                )
            )
            if not response.tool_calls:
                if not response.content.strip():
                    raise EmptyModelResponseError(response.profile_id)
                return self._finish(
                    AgentRunStatus.COMPLETED,
                    response.content,
                    step,
                    messages,
                    tool_results,
                    profile_ids,
                    context.items,
                    goal,
                    run_id,
                )
            if len(tool_results) + len(response.tool_calls) > max_tool_calls:
                return self._finish(
                    AgentRunStatus.TOOL_CALL_LIMIT,
                    "tool-call budget exhausted",
                    step,
                    messages,
                    tool_results,
                    profile_ids,
                    context.items,
                    goal,
                    run_id,
                )
            for model_call in response.tool_calls:
                executed = self.tool_executor.execute(goal, run_id, model_call)
                tool_results.append(executed.result)
                messages.append(executed.message)
            session = self.session.checkpoint(
                SessionProgress(
                    tuple(messages),
                    tuple(tool_results),
                    tuple(profile_ids),
                    step,
                    AgentRunStatus.ACTIVE,
                    "",
                    max_steps,
                    max_tool_calls,
                )
            )
        return self._finish(
            AgentRunStatus.STEP_LIMIT,
            "model-step budget exhausted",
            max_steps,
            messages,
            tool_results,
            profile_ids,
            context.items,
            goal,
            run_id,
        )

    def _finish(
        self,
        status: AgentRunStatus,
        output: str,
        steps: int,
        messages: list[ConversationMessage],
        tool_results: list[ToolResult],
        profile_ids: list[str],
        context_items: tuple[ContextItem, ...],
        goal: Goal,
        run_id: str,
    ) -> AgentRunResult:
        completed = status is AgentRunStatus.COMPLETED
        self.kernel.record(
            Event(
                (
                    EventType.AGENT_RUN_COMPLETED
                    if completed
                    else EventType.AGENT_RUN_LIMIT_REACHED
                ),
                Component.CONTROL_PLANE,
                trace_id=goal.goal_id,
                run_id=run_id,
                status=Status.OK if completed else Status.WARNING,
                summary=output,
                metrics={
                    "steps": float(steps),
                    "tool_calls": float(len(tool_results)),
                },
                metadata={"agent_status": status.value},
            )
        )
        active = self.session.state
        session = self.session.checkpoint(
            SessionProgress(
                tuple(messages),
                tuple(tool_results),
                tuple(profile_ids),
                steps,
                status,
                output,
                active.max_steps if active else steps,
                active.max_tool_calls if active else len(tool_results),
            )
        )
        return AgentRunResult(
            status,
            output,
            steps,
            tuple(messages),
            tuple(tool_results),
            tuple(profile_ids),
            (
                session.context_keys
                if session
                else tuple(item.key for item in context_items)
            ),
            str(session.session_id) if session else "",
            (
                str(session.parent_session_id)
                if session and session.parent_session_id
                else None
            ),
        )
