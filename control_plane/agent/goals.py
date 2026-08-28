from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Iterable, Optional

from .kernel import Goal


@dataclass(slots=True)  # noqa: MUTABLE_OK
class GoalState:
    """Mutable persisted progress record updated in place by GoalStore.save."""

    goal: Goal
    status: str = "active"
    current_state: str = "created"
    task_ids: tuple[str, ...] = ()
    unresolved_questions: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    updated_at: float = field(default_factory=time.time)


class GoalStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def save(self, state: GoalState) -> GoalState:
        records = {item.goal.goal_id: item for item in self.list()}
        state.updated_at = time.time()
        records[state.goal.goal_id] = state
        with self.path.open("w", encoding="utf-8") as stream:
            for item in records.values():
                stream.write(json.dumps(asdict(item), separators=(",", ":")) + "\n")
        return state

    def create(self, goal: Goal, task_ids: Iterable[str] = ()) -> GoalState:
        state = GoalState(goal, task_ids=tuple(task_ids))
        return self.save(state)

    def get(self, goal_id: str) -> Optional[GoalState]:
        return next(
            (item for item in self.list() if item.goal.goal_id == goal_id), None
        )

    def list(self) -> tuple[GoalState, ...]:
        if not self.path.exists():
            return ()
        items: list[GoalState] = []
        with self.path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                raw = json.loads(line)
                raw["task_ids"] = tuple(raw.get("task_ids", ()))
                raw["unresolved_questions"] = tuple(raw.get("unresolved_questions", ()))
                raw["evidence"] = tuple(raw.get("evidence", ()))
                raw["goal"]["success_conditions"] = tuple(
                    raw["goal"].get("success_conditions", ())
                )
                raw["goal"] = Goal(**raw["goal"])
                items.append(GoalState(**raw))
        return tuple(items)
