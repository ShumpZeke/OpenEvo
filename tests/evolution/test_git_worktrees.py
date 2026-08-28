import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import control_plane.agent as agent_module
from control_plane.telemetry.events import EventType


def _git(repository: Path, arguments: tuple[str, ...]) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repository(root: Path) -> Path:
    repository = root / "repository"
    repository.mkdir()
    _git(repository, ("init", "-b", "main"))
    _git(repository, ("config", "user.name", "OpenEvo Test"))
    _git(repository, ("config", "user.email", "openevo@example.invalid"))
    (repository / "candidate.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repository, ("add", "candidate.py"))
    _git(repository, ("commit", "-m", "baseline"))
    return repository


def test_worktree_world_diverges_without_modifying_baseline(tmp_path: Path) -> None:
    # Given
    repository = _repository(tmp_path)
    events = []
    manager = agent_module.GitWorktreeManager(
        repository,
        tmp_path / "worktrees",
        event_sink=events.append,
    )

    # When
    worktree = manager.create(
        agent_module.WorktreeSpec(agent_module.WorktreeId("candidate-a"))
    )
    world = agent_module.ExecutionWorld(str(worktree.path))
    world.write("candidate.py", "VALUE = 2\n")

    # Then
    assert (repository / "candidate.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert world.read("candidate.py") == "VALUE = 2\n"
    assert _git(worktree.path, ("branch", "--show-current")) == worktree.branch
    assert worktree.base_commit == _git(repository, ("rev-parse", "HEAD"))
    assert [event.type for event in events] == [EventType.WORKTREE_CREATED]


def test_concurrent_worktrees_isolate_candidate_changes(tmp_path: Path) -> None:
    # Given
    repository = _repository(tmp_path)
    manager = agent_module.GitWorktreeManager(repository, tmp_path / "worktrees")
    specs = (
        agent_module.WorktreeSpec(agent_module.WorktreeId("candidate-a")),
        agent_module.WorktreeSpec(agent_module.WorktreeId("candidate-b")),
    )

    # When
    with ThreadPoolExecutor(max_workers=2) as executor:
        worktrees = tuple(executor.map(manager.create, specs))
    for index, worktree in enumerate(worktrees, 2):
        agent_module.ExecutionWorld(str(worktree.path)).write(
            "candidate.py", f"VALUE = {index}\n"
        )

    # Then
    values = tuple(
        (worktree.path / "candidate.py").read_text(encoding="utf-8")
        for worktree in worktrees
    )
    assert values == ("VALUE = 2\n", "VALUE = 3\n")
    assert worktrees[0].path != worktrees[1].path
    assert (repository / "candidate.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_worktree_rejects_unsafe_candidate_identifier(tmp_path: Path) -> None:
    # Given
    manager = agent_module.GitWorktreeManager(
        _repository(tmp_path), tmp_path / "worktrees"
    )

    # When / Then
    with pytest.raises(agent_module.InvalidWorktreeIdError):
        manager.create(agent_module.WorktreeSpec(agent_module.WorktreeId("../escape")))


def test_git_repository_discovery_reports_repository_root(tmp_path: Path) -> None:
    # Given
    repository = _repository(tmp_path)
    nested = repository / "src" / "module"
    nested.mkdir(parents=True)

    # When
    discovered = agent_module.discover_git_repository(nested)

    # Then
    assert discovered == repository.resolve()


def test_worktree_removal_emits_lifecycle_event(tmp_path: Path) -> None:
    # Given
    events = []
    manager = agent_module.GitWorktreeManager(
        _repository(tmp_path),
        tmp_path / "worktrees",
        event_sink=events.append,
    )
    worktree = manager.create(
        agent_module.WorktreeSpec(agent_module.WorktreeId("candidate-a"))
    )

    # When
    manager.remove(worktree)

    # Then
    assert [event.type for event in events] == [
        EventType.WORKTREE_CREATED,
        EventType.WORKTREE_REMOVED,
    ]
