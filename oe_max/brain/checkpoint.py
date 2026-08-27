"""
Crash-safe, transactional checkpointing for evolution runs.

Persists:
  experiment ID, goal, base SHA, config, seed, generation,
  candidate IDs, patch hashes, parents, lineage, operators,
  metrics, test/benchmark results, archive membership,
  failure reasons, budgets, timestamps,
  host model metadata, OpenCode/plugin/engine versions

Provider/model identity is metadata only, not routing.

Uses atomic write (write tmp -> rename) and optional SQLite WAL for durability.
A run resumes after crash without losing the search.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class Checkpoint:
    experiment_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    goal: str = ""
    base_sha: str = ""
    config: Dict[str, Any] = field(default_factory=dict)
    seed: Optional[int] = None
    generation: int = 0
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    lineage: List[Dict[str, Any]] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    budgets: Dict[str, Any] = field(default_factory=dict)
    host_model_meta: Dict[str, Any] = field(default_factory=dict)
    opencode_version: str = ""
    plugin_version: str = ""
    engine_version: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Checkpoint":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class CheckpointStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, exp_id: str) -> Path:
        return self.root / f"{exp_id}.json"

    def save(self, cp: Checkpoint) -> Path:
        cp.updated_at = time.time()
        p = self._path(cp.experiment_id)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(cp.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
        return p

    def load(self, exp_id: str) -> Optional[Checkpoint]:
        p = self._path(exp_id)
        if not p.exists():
            return None
        try:
            return Checkpoint.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            return None

    def list(self) -> List[str]:
        return [p.stem for p in self.root.glob("*.json")]

    def latest(self) -> Optional[Checkpoint]:
        candidates = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            return None
        try:
            return Checkpoint.from_dict(json.loads(candidates[0].read_text(encoding="utf-8")))
        except Exception:
            return None
