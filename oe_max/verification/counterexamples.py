"""
Remembering what broke.

A candidate that fails verification is not just a candidate to discard — it is
evidence about the shape of the search space, and the input that broke it will
usually break the next candidate that makes the same mistake. Keeping those
inputs turns each failure into a permanent test.

Two operators in the taxonomy exist for exactly this and have nothing to work
with otherwise: `COUNTEREXAMPLE_REPAIR` and `ADVERSARIAL_REPAIR` are both gated
on `has_failure`, so without a store of failures they are never even offered.

Deduplicated by content, capped, and JSON on disk: this is a cache of evidence,
not a database. Losing it costs nothing except having to rediscover the same
failures, and it must never be able to block a run — a corrupt file reads as an
empty store.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional

# Enough to keep the recurring failures without turning a prompt into a
# transcript. The prompt budget is the real constraint: a reasoning model was
# measured spending 7,986 of an 8,000-token budget before writing anything.
DEFAULT_CAPACITY = 200


@dataclass
class Counterexample:
    """One input that made a candidate fail, and what it did instead."""

    check: str                        # which check caught it
    inputs: Any                       # what was fed in
    expected: Any = None
    actual: Any = None
    message: str = ""
    candidate_id: Optional[str] = None
    at: float = field(default_factory=time.time)
    hits: int = 1                     # how many candidates this has caught

    def fingerprint(self) -> str:
        """
        Identity is the check plus the input, not the message.

        Two candidates failing the same check on the same input are one piece
        of evidence seen twice — storing both would fill the store with the
        easiest failure to make.
        """
        payload = json.dumps({"check": self.check, "inputs": self.inputs},
                             sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def describe(self) -> str:
        """One line, for a prompt."""
        parts = [f"{self.check}: input={self.inputs!r}"]
        if self.expected is not None:
            parts.append(f"expected={self.expected!r}")
        if self.actual is not None:
            parts.append(f"got={self.actual!r}")
        if self.message:
            parts.append(self.message)
        return " · ".join(parts)


class CounterexampleStore:
    """Deduplicated, capped, persistent record of failing inputs."""

    def __init__(self, path: Optional[str] = None,
                 capacity: int = DEFAULT_CAPACITY) -> None:
        self.path = path
        self.capacity = capacity
        self._items: Dict[str, Counterexample] = {}
        if path:
            self.load()

    def __len__(self) -> int:
        return len(self._items)

    def add(self, ce: Counterexample) -> Counterexample:
        """Record a failure, or count another hit on one already known."""
        key = ce.fingerprint()
        existing = self._items.get(key)
        if existing is not None:
            existing.hits += 1
            existing.at = ce.at
            return existing
        self._items[key] = ce
        self._evict()
        return ce

    def extend(self, items: Iterable[Counterexample]) -> None:
        for ce in items:
            self.add(ce)

    def _evict(self) -> None:
        """
        Drop the least useful when full: fewest hits first, then oldest.

        Hits before age on purpose. A counterexample that has caught six
        candidates is describing a mistake this search keeps making, and is
        worth more than one that caught a single candidate an hour later.
        """
        if len(self._items) <= self.capacity:
            return
        ordered = sorted(self._items.items(), key=lambda kv: (kv[1].hits, kv[1].at))
        for key, _ in ordered[: len(self._items) - self.capacity]:
            del self._items[key]

    def most_valuable(self, limit: int = 5) -> List[Counterexample]:
        """The failures worth putting in front of a model, best first."""
        return sorted(self._items.values(),
                      key=lambda c: (c.hits, c.at), reverse=True)[:limit]

    def prompt_context(self, limit: int = 5) -> str:
        """
        The failure section of a repair prompt.

        Empty string when there is nothing, so a caller can test it plainly and
        an operator that needs a failure is not offered an empty one.
        """
        items = self.most_valuable(limit)
        if not items:
            return ""
        lines = ["Previously observed failures this program must not repeat:"]
        lines += [f"  - {c.describe()}" for c in items]
        return "\n".join(lines)

    # -- persistence ---------------------------------------------------

    def save(self, path: Optional[str] = None) -> None:
        target = path or self.path
        if not target:
            return
        os.makedirs(os.path.dirname(os.path.abspath(target)) or ".", exist_ok=True)
        tmp = f"{target}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump([c.to_dict() for c in self._items.values()], fh,
                      indent=2, default=str)
        # Atomic: a run killed mid-write must not leave a truncated store that
        # then reads as empty and silently loses every recorded failure.
        os.replace(tmp, target)

    def load(self, path: Optional[str] = None) -> None:
        target = path or self.path
        if not target or not os.path.exists(target):
            return
        try:
            with open(target, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            # Evidence, not correctness input: an unreadable store is an empty
            # store, never an error that stops a run.
            return
        if not isinstance(data, list):
            return
        for raw in data:
            if not isinstance(raw, dict) or "check" not in raw:
                continue
            self.add(Counterexample(
                check=raw.get("check", ""), inputs=raw.get("inputs"),
                expected=raw.get("expected"), actual=raw.get("actual"),
                message=raw.get("message", ""),
                candidate_id=raw.get("candidate_id"),
                at=float(raw.get("at") or time.time()),
                hits=int(raw.get("hits") or 1),
            ))
