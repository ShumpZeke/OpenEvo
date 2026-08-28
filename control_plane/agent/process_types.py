from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import NewType

ProcessSessionId = NewType("ProcessSessionId", str)


class ProcessState(str, Enum):
    RUNNING = "running"
    EXITED = "exited"
    TERMINATED = "terminated"


class ProcessStream(str, Enum):
    STDOUT = "stdout"
    STDERR = "stderr"


@dataclass(frozen=True, slots=True)
class ProcessSpec:
    program: str
    arguments: tuple[str, ...] = ()
    cwd: str = "."
    environment: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ProcessOutputChunk:
    sequence: int
    stream: ProcessStream
    text: str


@dataclass(frozen=True, slots=True)
class ProcessSnapshot:
    session_id: ProcessSessionId
    pid: int
    state: ProcessState
    returncode: int | None
    cursor: int
    chunks: tuple[ProcessOutputChunk, ...]
    truncated: bool = False
    timed_out: bool = False


class ProcessSessionError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ProcessWorkspaceEscapeError(ProcessSessionError):
    relative: str

    def __str__(self) -> str:
        return f"process working directory escapes execution world: {self.relative!r}"


@dataclass(frozen=True, slots=True)
class UnknownProcessSessionError(ProcessSessionError):
    session_id: ProcessSessionId

    def __str__(self) -> str:
        return f"unknown process session: {self.session_id}"


@dataclass(frozen=True, slots=True)
class ProcessInputClosedError(ProcessSessionError):
    session_id: ProcessSessionId

    def __str__(self) -> str:
        return f"stdin is closed for process session: {self.session_id}"


@dataclass(frozen=True, slots=True)
class ProcessTerminationError(ProcessSessionError):
    session_id: ProcessSessionId
    pid: int

    def __str__(self) -> str:
        return f"could not terminate process tree {self.pid} for {self.session_id}"
