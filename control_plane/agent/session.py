from __future__ import annotations

import os
import time
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from threading import Lock
from typing import Callable, NewType

from pydantic import TypeAdapter, ValidationError

from ..providers.profiles import Role
from ..telemetry.events import Component, Event, EventType, Status, new_id
from ..telemetry.redaction import Redactor, default_redactor
from .kernel import Goal, ToolResult
from .model import ConversationMessage, ModelToolArgument, ModelToolCall

AgentSessionId = NewType("AgentSessionId", str)


class AgentRunStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    STEP_LIMIT = "step_limit"
    TOOL_CALL_LIMIT = "tool_call_limit"


@dataclass(frozen=True, slots=True)
class SessionStart:
    goal: Goal
    role: Role
    context_keys: tuple[str, ...]
    messages: tuple[ConversationMessage, ...]
    max_steps: int
    max_tool_calls: int
    run_id: str


@dataclass(frozen=True, slots=True)
class SessionProgress:
    messages: tuple[ConversationMessage, ...]
    tool_results: tuple[ToolResult, ...]
    profile_ids: tuple[str, ...]
    steps: int
    status: AgentRunStatus
    output: str
    max_steps: int
    max_tool_calls: int


@dataclass(frozen=True, slots=True)
class AgentSessionState:
    session_id: AgentSessionId
    parent_session_id: AgentSessionId | None
    sequence: int
    goal: Goal
    role: Role
    context_keys: tuple[str, ...]
    messages: tuple[ConversationMessage, ...]
    tool_results: tuple[ToolResult, ...]
    profile_ids: tuple[str, ...]
    steps: int
    status: AgentRunStatus
    output: str
    max_steps: int
    max_tool_calls: int
    run_id: str
    created_at: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class SessionNotFoundError(LookupError):
    session_id: AgentSessionId

    def __str__(self) -> str:
        return f"agent session {self.session_id!r} does not exist"


@dataclass(frozen=True, slots=True)
class SessionConflictError(RuntimeError):
    session_id: AgentSessionId
    expected_sequence: int
    actual_sequence: int

    def __str__(self) -> str:
        return (
            f"agent session {self.session_id!r} changed from sequence "
            f"{self.expected_sequence} to {self.actual_sequence}"
        )


@dataclass(frozen=True, slots=True)
class InvalidSessionRecordError(RuntimeError):
    path: Path
    detail: str

    def __str__(self) -> str:
        return f"invalid agent session record {self.path}: {self.detail}"


_SESSION_ADAPTER = TypeAdapter(AgentSessionState)
SessionEventSink = Callable[[Event], None]


class AgentSessionStore:
    def __init__(
        self,
        root: str,
        redactor: Redactor | None = None,
        event_sink: SessionEventSink | None = None,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._redactor = redactor or default_redactor()
        self._lock = Lock()
        self._event_sink = event_sink

    def create(self, start: SessionStart) -> AgentSessionState:
        now = time.time()
        state = AgentSessionState(
            AgentSessionId(new_id("session_")),
            None,
            0,
            start.goal,
            start.role,
            start.context_keys,
            start.messages,
            (),
            (),
            0,
            AgentRunStatus.ACTIVE,
            "",
            start.max_steps,
            start.max_tool_calls,
            start.run_id,
            now,
            now,
        )
        with self._lock:
            self._write(state)
        created = self.load(state.session_id)
        self._emit(EventType.AGENT_SESSION_CREATED, created, "agent session created")
        return created

    def load(self, session_id: str | AgentSessionId) -> AgentSessionState:
        branded_id = AgentSessionId(str(session_id))
        path = self._path(branded_id)
        if not path.is_file():
            raise SessionNotFoundError(branded_id)
        try:
            return _SESSION_ADAPTER.validate_json(path.read_bytes())
        except ValidationError as exc:
            raise InvalidSessionRecordError(path, str(exc)) from exc

    def checkpoint(
        self,
        state: AgentSessionState,
        progress: SessionProgress,
    ) -> AgentSessionState:
        with self._lock:
            current = self.load(state.session_id)
            if current.sequence != state.sequence:
                raise SessionConflictError(
                    state.session_id,
                    state.sequence,
                    current.sequence,
                )
            updated = replace(
                state,
                sequence=state.sequence + 1,
                messages=progress.messages,
                tool_results=progress.tool_results,
                profile_ids=progress.profile_ids,
                steps=progress.steps,
                status=progress.status,
                output=progress.output,
                max_steps=progress.max_steps,
                max_tool_calls=progress.max_tool_calls,
                updated_at=time.time(),
            )
            self._write(updated)
        return self.load(updated.session_id)

    def fork(self, session_id: str | AgentSessionId) -> AgentSessionState:
        parent = self.load(session_id)
        now = time.time()
        child = replace(
            parent,
            session_id=AgentSessionId(new_id("session_")),
            parent_session_id=parent.session_id,
            sequence=0,
            status=AgentRunStatus.ACTIVE,
            output="",
            created_at=now,
            updated_at=now,
            run_id=new_id("agent_run_"),
        )
        with self._lock:
            self._write(child)
        forked = self.load(child.session_id)
        self._emit(EventType.AGENT_SESSION_FORKED, forked, "agent session forked")
        return forked

    def record_resumed(self, state: AgentSessionState) -> None:
        self._emit(EventType.AGENT_SESSION_RESUMED, state, "agent session resumed")

    def _path(self, session_id: AgentSessionId) -> Path:
        return self.root / f"{session_id}.json"

    def _write(self, state: AgentSessionState) -> None:
        sanitized = self._sanitize(state)
        path = self._path(state.session_id)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(_SESSION_ADAPTER.dump_json(sanitized))
        os.replace(temporary, path)

    def _sanitize(self, state: AgentSessionState) -> AgentSessionState:
        redact = self._redactor.redact_text
        goal = replace(
            state.goal,
            objective=redact(state.goal.objective),
            success_conditions=tuple(
                redact(item) for item in state.goal.success_conditions
            ),
        )
        messages = tuple(self._sanitize_message(message) for message in state.messages)
        tool_results = tuple(
            replace(
                result,
                output=redact(result.output),
                error=redact(result.error) if result.error is not None else None,
            )
            for result in state.tool_results
        )
        return replace(state, goal=goal, messages=messages, tool_results=tool_results)

    def _sanitize_message(self, message: ConversationMessage) -> ConversationMessage:
        redact = self._redactor.redact_text
        calls = tuple(
            replace(
                call,
                arguments=tuple(
                    ModelToolArgument(argument.name, redact(argument.value))
                    for argument in call.arguments
                ),
            )
            for call in message.tool_calls
        )
        return replace(message, content=redact(message.content), tool_calls=calls)

    def _emit(
        self,
        event_type: EventType,
        state: AgentSessionState,
        summary: str,
    ) -> None:
        if self._event_sink is None:
            return
        self._event_sink(
            Event(
                event_type,
                Component.CONTROL_PLANE,
                trace_id=state.goal.goal_id,
                run_id=state.run_id,
                status=Status.RUNNING,
                summary=summary,
                metadata={
                    "session_id": str(state.session_id),
                    "parent_session_id": (
                        str(state.parent_session_id)
                        if state.parent_session_id is not None
                        else None
                    ),
                    "sequence": state.sequence,
                },
            )
        )
