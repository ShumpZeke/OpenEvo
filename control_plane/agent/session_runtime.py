from __future__ import annotations

from dataclasses import dataclass

from .session import (
    AgentSessionState,
    AgentSessionStore,
    SessionProgress,
    SessionStart,
)


@dataclass(frozen=True, slots=True)
class SessionGoalMismatchError(ValueError):
    session_goal_id: str
    requested_goal_id: str

    def __str__(self) -> str:
        return (
            f"agent session belongs to goal {self.session_goal_id!r}, "
            f"not {self.requested_goal_id!r}"
        )


@dataclass(frozen=True, slots=True)
class InvalidResumeBudgetError(ValueError):
    completed_steps: int
    completed_tool_calls: int
    requested_steps: int
    requested_tool_calls: int

    def __str__(self) -> str:
        return (
            "resume budgets cannot be lower than completed work: "
            f"steps={self.completed_steps}/{self.requested_steps}, "
            f"tool_calls={self.completed_tool_calls}/{self.requested_tool_calls}"
        )


class AgentSessionCoordinator:
    def __init__(
        self,
        store: AgentSessionStore | None,
        session_id: str | None,
    ) -> None:
        self.store = store
        self.session_id = session_id
        self.state: AgentSessionState | None = None

    def start(self, start: SessionStart) -> AgentSessionState | None:
        if self.store is None:
            return None
        if self.session_id is None:
            self.state = self.store.create(start)
            return self.state
        state = self.store.load(self.session_id)
        if state.goal.goal_id != start.goal.goal_id:
            raise SessionGoalMismatchError(state.goal.goal_id, start.goal.goal_id)
        if start.max_steps < state.steps or start.max_tool_calls < len(
            state.tool_results
        ):
            raise InvalidResumeBudgetError(
                state.steps,
                len(state.tool_results),
                start.max_steps,
                start.max_tool_calls,
            )
        self.state = state
        self.store.record_resumed(state)
        return state

    def checkpoint(self, progress: SessionProgress) -> AgentSessionState | None:
        if self.store is None or self.state is None:
            return self.state
        self.state = self.store.checkpoint(self.state, progress)
        return self.state
