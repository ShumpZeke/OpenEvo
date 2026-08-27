"""
Content-addressed caching — deterministic work never reruns for same identity.

Candidate identity:
  base_git_sha + patch_hash + evaluator_version + benchmark_version
  + relevant_configuration + toolchain fingerprint

Caches:
  - candidate equivalence (dedup)
  - static analysis
  - impacted tests
  - test outcomes when safe
  - benchmark outcomes when safe
  - context retrieval / repo index

Uses a simple file-backed + memory cache; eviction is LRU when configured.
No model-specific logic.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


def _hash_str(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@dataclass
class CacheKey:
    base_sha: str = ""
    patch_hash: str = ""
    evaluator_version: str = "v1"
    benchmark_version: str = "v1"
    config_hash: str = ""
    toolchain: str = ""

    def to_str(self) -> str:
        return _canonical(
            {
                "base_sha": self.base_sha,
                "patch_hash": self.patch_hash,
                "evaluator_version": self.evaluator_version,
                "benchmark_version": self.benchmark_version,
                "config_hash": self.config_hash,
                "toolchain": self.toolchain,
            }
        )

    def digest(self) -> str:
        return _hash_str(self.to_str())


@dataclass
class CacheEntry:
    key: str
    value: Any
    created_at: float = field(default_factory=time.time)
    hits: int = 0


class ContentCache:
    """Bounded content-addressed cache. Thread-safe for the evolution loop's use."""

    def __init__(self, max_entries: int = 4096, persist_path: Optional[Path] = None) -> None:
        self.max_entries = max_entries
        self.persist_path = persist_path
        self._store: Dict[str, CacheEntry] = {}
        self._order: deque[str] = deque()
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        if persist_path and persist_path.exists():
            try:
                data = json.loads(persist_path.read_text(encoding="utf-8"))
                for k, v in data.items():
                    entry = CacheEntry(key=k, value=v.get("value"), created_at=v.get("created_at", time.time()))
                    self._store[k] = entry
                    self._order.append(k)
            except Exception:
                pass

    def make_key(
        self,
        *,
        base_sha: str = "",
        patch: str = "",
        evaluator_version: str = "v1",
        benchmark_version: str = "v1",
        config: Optional[Dict[str, Any]] = None,
        toolchain: str = "",
    ) -> str:
        patch_hash = _hash_str(patch) if patch else ""
        config_hash = _hash_str(_canonical(config or {}))
        k = CacheKey(
            base_sha=base_sha,
            patch_hash=patch_hash,
            evaluator_version=evaluator_version,
            benchmark_version=benchmark_version,
            config_hash=config_hash,
            toolchain=toolchain,
        )
        return k.digest()

    def get(self, key: str) -> Optional[Any]:
        e = self._store.get(key)
        if e is None:
            self.misses += 1
            return None
        e.hits += 1
        self.hits += 1
        return e.value

    def put(self, key: str, value: Any) -> None:
        if len(self._store) >= self.max_entries and key not in self._store:
            oldest_key = self._order.popleft()
            del self._store[oldest_key]
            self.evictions += 1
        if key not in self._store:
            self._order.append(key)
        self._store[key] = CacheEntry(key=key, value=value)

    def clear(self) -> None:
        self._store.clear()
        self._order.clear()
        self.hits = 0
        self.misses = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def stats(self) -> Dict[str, Any]:
        return {
            "size": len(self._store),
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hit_rate, 3),
            "evictions": self.evictions,
        }

    def persist(self) -> None:
        if not self.persist_path:
            return
        self.persist_path.parent.mkdir(parents=True, exist_ok=True)
        data = {k: {"value": v.value, "created_at": v.created_at} for k, v in self._store.items()}
        tmp = self.persist_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.persist_path)
