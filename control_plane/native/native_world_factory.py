from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from control_plane.agent import (
    AgentKernel,
    AgentSessionStore,
    EventSink,
    ExecutionWorld,
    GitWorktreeManager,
    GoalStore,
    LineageMemory,
    NativeAgentRuntime,
    WorktreeId,
    WorktreeSpec,
    native_world_tools,
)
from .native_evolution_types import NativeEvolutionSettings


@dataclass(frozen=True, slots=True)
class NativeWorldServices:
    settings: NativeEvolutionSettings
    memory: LineageMemory
    goals: GoalStore
    sessions: AgentSessionStore
    event_sink: EventSink


class NativeWorldFactory:
    def __init__(self, services: NativeWorldServices) -> None:
        self.services = services
        settings = services.settings
        self.worktrees = (
            GitWorktreeManager(
                settings.repository_root,
                self._worktree_root(settings),
                event_sink=services.event_sink,
            )
            if settings.repository_root is not None
            else None
        )

    def create(self, candidate_id: str) -> NativeAgentRuntime:
        world = ExecutionWorld(str(self._world_path(candidate_id)))
        services = self.services
        runtime = NativeAgentRuntime(
            world,
            kernel=AgentKernel(event_sink=services.event_sink),
            memory=services.memory,
            goals=services.goals,
            sessions=services.sessions,
        )
        for tool in native_world_tools(world):
            runtime.register_tool(tool)
        return runtime

    def _world_path(self, candidate_id: str) -> Path:
        if self.worktrees is None:
            return self.services.settings.output_dir / "agent_worlds" / candidate_id
        return self.worktrees.create(
            WorktreeSpec(
                WorktreeId(candidate_id),
                self.services.settings.worktree_base_ref,
            )
        ).path

    @staticmethod
    def _worktree_root(settings: NativeEvolutionSettings) -> Path:
        if settings.worktree_root is not None:
            return settings.worktree_root
        repository = settings.repository_root
        if repository is None:
            return settings.output_dir / "agent_worktrees"
        return (
            repository.resolve().parent
            / ".openevo-worktrees"
            / repository.name
            / settings.output_dir.name
        )
