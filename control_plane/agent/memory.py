from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, Iterator, Optional


@dataclass(frozen=True, slots=True)
class KnowledgeItem:
    kind: str
    statement: str
    source_id: str
    score: Optional[float] = None


class LineageMemory:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def remember(self, item: KnowledgeItem) -> None:
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(asdict(item), separators=(",", ":")) + "\n")

    def inherit(
        self, parent_ids: Iterable[str], limit: int = 32
    ) -> tuple[KnowledgeItem, ...]:
        wanted = set(parent_ids)
        if not wanted or not self.path.exists():
            return ()
        items: list[KnowledgeItem] = []
        with self.path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                raw = json.loads(line)
                if raw.get("source_id") in wanted:
                    items.append(KnowledgeItem(**raw))
        unique: dict[tuple[str, str], KnowledgeItem] = {}
        for item in items:
            unique[(item.kind, item.statement)] = item
        return tuple(list(unique.values())[-limit:])

    def all(self) -> Iterator[KnowledgeItem]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    yield KnowledgeItem(**json.loads(line))
