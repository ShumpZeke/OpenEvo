from __future__ import annotations

import os
import json
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from .kernel import Tool, ToolCall, ToolResult
from .process_tools import native_process_tools
from .processes import ProcessSessionManager


@dataclass(frozen=True, slots=True)
class ProcessResult:
    command: str
    returncode: Optional[int]
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: float


@dataclass(frozen=True, slots=True)
class WorkspaceEscapeError(ValueError):
    relative: str

    def __str__(self) -> str:
        return f"path escapes execution world: {self.relative!r}"


class ExecutionWorld:
    def __init__(self, root: Optional[str] = None) -> None:
        self.root = Path(root or tempfile.mkdtemp(prefix="openevo-world-")).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.processes = ProcessSessionManager(self.root)

    def path(self, relative: str) -> Path:
        candidate = (self.root / relative).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise WorkspaceEscapeError(relative)
        return candidate

    def read(self, relative: str) -> str:
        return self.path(relative).read_text(encoding="utf-8")

    def write(self, relative: str, content: str) -> str:
        target = self.path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return str(target.relative_to(self.root))

    def run(
        self,
        command: str,
        timeout_s: float = 30.0,
        environment: Optional[Mapping[str, str]] = None,
    ) -> ProcessResult:
        started = time.perf_counter()
        env = os.environ.copy()
        env.update(environment or {})
        try:
            completed = subprocess.run(
                command,
                cwd=self.root,
                env=env,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
            return ProcessResult(
                command,
                completed.returncode,
                completed.stdout,
                completed.stderr,
                False,
                (time.perf_counter() - started) * 1000,
            )
        except subprocess.TimeoutExpired as exc:
            return ProcessResult(
                command,
                None,
                _text(exc.stdout),
                _text(exc.stderr),
                True,
                (time.perf_counter() - started) * 1000,
            )


def _text(value: bytes | str | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _tool_error_name(error: OSError | ValueError) -> str:
    if isinstance(error, WorkspaceEscapeError):
        return "ValueError"
    return type(error).__name__


class ReadFileTool:
    name = "read_file"

    def __init__(self, world: ExecutionWorld) -> None:
        self.world = world

    def invoke(self, call: ToolCall) -> ToolResult:
        relative = call.arguments.get("path", "")
        started = time.perf_counter()
        try:
            return ToolResult(
                self.name,
                True,
                self.world.read(relative),
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        except (OSError, ValueError) as exc:
            return ToolResult(
                self.name,
                False,
                error=_tool_error_name(exc),
                duration_ms=(time.perf_counter() - started) * 1000,
            )


class WriteFileTool:
    name = "write_file"

    def __init__(self, world: ExecutionWorld) -> None:
        self.world = world

    def invoke(self, call: ToolCall) -> ToolResult:
        started = time.perf_counter()
        try:
            path = self.world.write(
                call.arguments.get("path", ""), call.arguments.get("content", "")
            )
            return ToolResult(
                self.name,
                True,
                path,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        except (OSError, ValueError) as exc:
            return ToolResult(
                self.name,
                False,
                error=_tool_error_name(exc),
                duration_ms=(time.perf_counter() - started) * 1000,
            )


class ShellTool:
    name = "shell"

    def __init__(self, world: ExecutionWorld) -> None:
        self.world = world

    def invoke(self, call: ToolCall) -> ToolResult:
        command = call.arguments.get("command", "")
        timeout = float(call.arguments.get("timeout_s", "30"))
        result = self.world.run(command, timeout)
        output = result.stdout
        if result.stderr:
            output += ("\n" if output else "") + result.stderr
        ok = result.returncode == 0 and not result.timed_out
        return ToolResult(
            self.name,
            ok,
            output,
            error="timeout" if result.timed_out else None,
            duration_ms=result.duration_ms,
        )


class GlobTool:
    name = "glob"

    def __init__(self, world: ExecutionWorld) -> None:
        self.world = world

    def invoke(self, call: ToolCall) -> ToolResult:
        pattern = call.arguments.get("pattern", "*")
        try:
            matches = [
                path.relative_to(self.world.root).as_posix()
                for path in self.world.root.glob(pattern)
            ]
            return ToolResult(self.name, True, json.dumps(matches))
        except (OSError, ValueError) as exc:
            return ToolResult(self.name, False, error=_tool_error_name(exc))


class SearchTextTool:
    name = "search_text"

    def __init__(self, world: ExecutionWorld) -> None:
        self.world = world

    def invoke(self, call: ToolCall) -> ToolResult:
        needle = call.arguments.get("query", "")
        pattern = call.arguments.get("pattern", "**/*")
        if not needle:
            return ToolResult(self.name, False, error="query is required")
        matches: list[str] = []
        try:
            for path in self.world.root.glob(pattern):
                if path.is_file():
                    try:
                        for line_number, line in enumerate(
                            path.read_text(encoding="utf-8").splitlines(), 1
                        ):
                            if needle in line:
                                matches.append(
                                    f"{path.relative_to(self.world.root).as_posix()}:{line_number}:{line}"
                                )
                    except (OSError, UnicodeError):
                        continue
            return ToolResult(self.name, True, "\n".join(matches))
        except (OSError, ValueError) as exc:
            return ToolResult(self.name, False, error=_tool_error_name(exc))


class MetadataTool:
    name = "file_metadata"

    def __init__(self, world: ExecutionWorld) -> None:
        self.world = world

    def invoke(self, call: ToolCall) -> ToolResult:
        try:
            path = self.world.path(call.arguments.get("path", ""))
            stat = path.stat()
            return ToolResult(
                self.name,
                True,
                json.dumps(
                    {
                        "path": path.relative_to(self.world.root).as_posix(),
                        "is_file": path.is_file(),
                        "is_dir": path.is_dir(),
                        "size": stat.st_size,
                        "modified_ns": stat.st_mtime_ns,
                    }
                ),
            )
        except (OSError, ValueError) as exc:
            return ToolResult(self.name, False, error=_tool_error_name(exc))


class GitTool:
    def __init__(self, world: ExecutionWorld, name: str, operation: str) -> None:
        self.world = world
        self.name = name
        self.operation = operation

    def invoke(self, call: ToolCall) -> ToolResult:
        result = self.world.run(f"git {self.operation}", timeout_s=30)
        output = result.stdout + (
            ("\n" if result.stdout else "") + result.stderr if result.stderr else ""
        )
        return ToolResult(
            self.name,
            result.returncode == 0 and not result.timed_out,
            output,
            "timeout" if result.timed_out else None,
            result.duration_ms,
        )


def native_world_tools(world: ExecutionWorld) -> tuple[Tool, ...]:
    return (
        ReadFileTool(world),
        WriteFileTool(world),
        ShellTool(world),
        GlobTool(world),
        SearchTextTool(world),
        MetadataTool(world),
        GitTool(world, "git_status", "status --short"),
        GitTool(world, "git_diff_stat", "diff --stat"),
        *native_process_tools(world.processes),
    )
