from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, Sequence

if TYPE_CHECKING:
    from .kernel import AgentTask


@dataclass(frozen=True, slots=True)
class RoleProfile:
    name: str
    capabilities: frozenset[str]
    priority: int = 100


@dataclass(frozen=True, slots=True)
class RoleAssignment:
    task_id: str
    role: str


@dataclass(frozen=True, slots=True)
class NoEligibleRoleError(Exception):
    task_id: str
    required_capabilities: tuple[str, ...]

    def __str__(self) -> str:
        required = ", ".join(self.required_capabilities)
        return f"task {self.task_id} has no role providing: {required}"


class TaskDelegator(Protocol):
    def assign(self, task: AgentTask) -> RoleAssignment: ...


class DelegationPlanner:
    def __init__(self, profiles: Sequence[RoleProfile]) -> None:
        self._profiles = tuple(profiles)

    def assign(self, task: AgentTask) -> RoleAssignment:
        required = frozenset(task.required_capabilities)
        eligible = tuple(
            profile
            for profile in self._profiles
            if required.issubset(profile.capabilities)
        )
        if not eligible:
            raise NoEligibleRoleError(task.task_id, task.required_capabilities)
        selected = min(eligible, key=lambda profile: profile.priority)
        return RoleAssignment(task.task_id, selected.name)
