from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Callable, Final, NewType

from ..telemetry.events import Component, Event, EventType, Status

WorktreeId = NewType("WorktreeId", str)
WorktreeEventSink = Callable[[Event], None]
_SAFE_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


@dataclass(frozen=True, slots=True)
class WorktreeSpec:
    worktree_id: WorktreeId
    base_ref: str = "HEAD"


@dataclass(frozen=True, slots=True)
class GitWorktree:
    worktree_id: WorktreeId
    path: Path
    branch: str
    base_commit: str


@dataclass(frozen=True, slots=True)
class GitCommandResult:
    arguments: tuple[str, ...]
    cwd: Path
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class GitExecutableNotFoundError(RuntimeError):
    def __str__(self) -> str:
        return "Git executable is unavailable"


@dataclass(frozen=True, slots=True)
class NotGitRepositoryError(ValueError):
    path: Path

    def __str__(self) -> str:
        return f"path is not inside a Git repository: {self.path}"


@dataclass(frozen=True, slots=True)
class InvalidWorktreeIdError(ValueError):
    worktree_id: WorktreeId

    def __str__(self) -> str:
        return f"unsafe worktree identifier: {self.worktree_id!r}"


@dataclass(frozen=True, slots=True)
class WorktreeRootInsideRepositoryError(ValueError):
    repository: Path
    worktree_root: Path

    def __str__(self) -> str:
        return f"worktree root {self.worktree_root} must be outside {self.repository}"


@dataclass(frozen=True, slots=True)
class GitWorktreeCommandError(RuntimeError):
    result: GitCommandResult

    def __str__(self) -> str:
        detail = self.result.stderr.strip() or self.result.stdout.strip()
        command = " ".join(self.result.arguments)
        return f"Git command failed ({self.result.returncode}): {command}: {detail}"


@dataclass(frozen=True, slots=True)
class WorktreeOwnershipError(ValueError):
    path: Path

    def __str__(self) -> str:
        return f"worktree is not managed by this manager: {self.path}"


def _run_git(cwd: Path, arguments: tuple[str, ...]) -> GitCommandResult:
    try:
        completed = subprocess.run(
            ("git", "-C", str(cwd), *arguments),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as error:
        raise GitExecutableNotFoundError() from error
    return GitCommandResult(
        arguments,
        cwd,
        completed.returncode,
        completed.stdout,
        completed.stderr,
    )


def discover_git_repository(start: Path) -> Path | None:
    result = _run_git(start.resolve(), ("rev-parse", "--show-toplevel"))
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


class GitWorktreeManager:
    def __init__(
        self,
        repository: Path,
        worktree_root: Path,
        event_sink: WorktreeEventSink | None = None,
    ) -> None:
        discovered = discover_git_repository(repository)
        if discovered is None:
            raise NotGitRepositoryError(repository)
        resolved_root = worktree_root.resolve()
        if resolved_root == discovered or discovered in resolved_root.parents:
            raise WorktreeRootInsideRepositoryError(discovered, resolved_root)
        self.repository = discovered
        self.worktree_root = resolved_root
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        self.event_sink = event_sink
        self._lock = Lock()

    def create(self, spec: WorktreeSpec) -> GitWorktree:
        identifier = str(spec.worktree_id)
        if _SAFE_ID.fullmatch(identifier) is None:
            raise InvalidWorktreeIdError(spec.worktree_id)
        with self._lock:
            base_commit = self._commit(spec.base_ref)
            path = (self.worktree_root / identifier).resolve()
            branch = f"openevo/{identifier}"
            result = _run_git(
                self.repository,
                ("worktree", "add", "-b", branch, str(path), base_commit),
            )
            if result.returncode != 0:
                raise GitWorktreeCommandError(result)
        worktree = GitWorktree(spec.worktree_id, path, branch, base_commit)
        self._emit_created(worktree)
        return worktree

    def remove(self, worktree: GitWorktree, force: bool = False) -> None:
        expected = (self.worktree_root / str(worktree.worktree_id)).resolve()
        if worktree.path.resolve() != expected:
            raise WorktreeOwnershipError(worktree.path)
        arguments = (
            ("worktree", "remove", "--force", str(worktree.path))
            if force
            else ("worktree", "remove", str(worktree.path))
        )
        with self._lock:
            result = _run_git(self.repository, arguments)
            if result.returncode != 0:
                raise GitWorktreeCommandError(result)
        if self.event_sink is not None:
            self.event_sink(
                Event(
                    EventType.WORKTREE_REMOVED,
                    Component.SANDBOX,
                    candidate_id=str(worktree.worktree_id),
                    status=Status.OK,
                    summary="candidate worktree removed",
                    metadata={
                        "path": str(worktree.path),
                        "branch": worktree.branch,
                        "base_commit": worktree.base_commit,
                    },
                )
            )

    def _commit(self, base_ref: str) -> str:
        result = _run_git(
            self.repository,
            ("rev-parse", "--verify", f"{base_ref}^{{commit}}"),
        )
        if result.returncode != 0:
            raise GitWorktreeCommandError(result)
        return result.stdout.strip()

    def _emit_created(self, worktree: GitWorktree) -> None:
        if self.event_sink is None:
            return
        self.event_sink(
            Event(
                EventType.WORKTREE_CREATED,
                Component.SANDBOX,
                candidate_id=str(worktree.worktree_id),
                status=Status.OK,
                summary="candidate worktree created",
                metadata={
                    "path": str(worktree.path),
                    "branch": worktree.branch,
                    "base_commit": worktree.base_commit,
                },
            )
        )
