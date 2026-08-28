from __future__ import annotations

import json
import time
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from .kernel import Tool, ToolCall, ToolResult
from .process_types import (
    ProcessSessionError,
    ProcessSessionId,
    ProcessSnapshot,
    ProcessSpec,
)
from .processes import ProcessSessionManager

_ARGUMENTS: Final = TypeAdapter(tuple[str, ...])
_ENVIRONMENT: Final = TypeAdapter(dict[str, str])


class _ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _StartArguments(_ToolArguments):
    program: str = Field(min_length=1)
    arguments_json: str = "[]"
    cwd: str = "."
    environment_json: str = "{}"


class _SessionArguments(_ToolArguments):
    session_id: str = Field(min_length=1)


class _ReadArguments(_SessionArguments):
    cursor: int = Field(default=0, ge=0)


class _WriteArguments(_SessionArguments):
    data: str


class _WaitArguments(_SessionArguments):
    timeout_s: float = Field(default=30.0, gt=0.0, le=300.0)


class _TerminateArguments(_SessionArguments):
    grace_s: float = Field(default=2.0, ge=0.1, le=30.0)


class ProcessStartTool:
    name = "process_start"

    def __init__(self, manager: ProcessSessionManager) -> None:
        self.manager = manager

    def invoke(self, call: ToolCall) -> ToolResult:
        started = time.perf_counter()
        try:
            arguments = _StartArguments.model_validate(call.arguments)
            snapshot = self.manager.start(
                ProcessSpec(
                    arguments.program,
                    _ARGUMENTS.validate_json(arguments.arguments_json),
                    arguments.cwd,
                    tuple(
                        _ENVIRONMENT.validate_json(arguments.environment_json).items()
                    ),
                )
            )
            return _result(self.name, started, snapshot)
        except (ValidationError, OSError, ProcessSessionError) as error:
            return _failure(self.name, started, error)


class ProcessReadTool:
    name = "process_read"

    def __init__(self, manager: ProcessSessionManager) -> None:
        self.manager = manager

    def invoke(self, call: ToolCall) -> ToolResult:
        started = time.perf_counter()
        try:
            arguments = _ReadArguments.model_validate(call.arguments)
            snapshot = self.manager.read(
                ProcessSessionId(arguments.session_id), arguments.cursor
            )
            return _result(self.name, started, snapshot)
        except (ValidationError, ProcessSessionError) as error:
            return _failure(self.name, started, error)


class ProcessWriteTool:
    name = "process_write"

    def __init__(self, manager: ProcessSessionManager) -> None:
        self.manager = manager

    def invoke(self, call: ToolCall) -> ToolResult:
        started = time.perf_counter()
        try:
            arguments = _WriteArguments.model_validate(call.arguments)
            snapshot = self.manager.write(
                ProcessSessionId(arguments.session_id), arguments.data
            )
            return _result(self.name, started, snapshot)
        except (ValidationError, ProcessSessionError) as error:
            return _failure(self.name, started, error)


class ProcessWaitTool:
    name = "process_wait"

    def __init__(self, manager: ProcessSessionManager) -> None:
        self.manager = manager

    def invoke(self, call: ToolCall) -> ToolResult:
        started = time.perf_counter()
        try:
            arguments = _WaitArguments.model_validate(call.arguments)
            snapshot = self.manager.wait(
                ProcessSessionId(arguments.session_id), arguments.timeout_s
            )
            return _result(self.name, started, snapshot)
        except (ValidationError, ProcessSessionError) as error:
            return _failure(self.name, started, error)


class ProcessTerminateTool:
    name = "process_terminate"

    def __init__(self, manager: ProcessSessionManager) -> None:
        self.manager = manager

    def invoke(self, call: ToolCall) -> ToolResult:
        started = time.perf_counter()
        try:
            arguments = _TerminateArguments.model_validate(call.arguments)
            snapshot = self.manager.terminate(
                ProcessSessionId(arguments.session_id), arguments.grace_s
            )
            return _result(self.name, started, snapshot)
        except (ValidationError, ProcessSessionError) as error:
            return _failure(self.name, started, error)


def native_process_tools(manager: ProcessSessionManager) -> tuple[Tool, ...]:
    return (
        ProcessStartTool(manager),
        ProcessReadTool(manager),
        ProcessWriteTool(manager),
        ProcessWaitTool(manager),
        ProcessTerminateTool(manager),
    )


def _result(
    name: str,
    started: float,
    snapshot: ProcessSnapshot,
) -> ToolResult:
    payload = {
        "session_id": snapshot.session_id,
        "pid": snapshot.pid,
        "state": snapshot.state.value,
        "returncode": snapshot.returncode,
        "cursor": snapshot.cursor,
        "truncated": snapshot.truncated,
        "timed_out": snapshot.timed_out,
        "chunks": [
            {
                "sequence": chunk.sequence,
                "stream": chunk.stream.value,
                "text": chunk.text,
            }
            for chunk in snapshot.chunks
        ],
    }
    return ToolResult(
        name,
        not snapshot.timed_out,
        json.dumps(payload, separators=(",", ":")),
        error="timeout" if snapshot.timed_out else None,
        duration_ms=(time.perf_counter() - started) * 1000,
    )


def _failure(
    name: str,
    started: float,
    error: ValidationError | OSError | ProcessSessionError,
) -> ToolResult:
    return ToolResult(
        name,
        False,
        error=type(error).__name__,
        duration_ms=(time.perf_counter() - started) * 1000,
    )
