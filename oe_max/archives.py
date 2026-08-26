"""
Research archives.

Upstream keeps a MAP-Elites grid and a single best program. That is enough to
run evolution and not enough to do research on it: it answers "what is the best
score" and nothing about trade-offs, about what has already been tried, or about
how a candidate failed.

Four archives, each answering a question the others cannot:

  HallOfFame          which candidates were ever champion, and when
  ParetoArchive       what is optimal when metrics conflict
  NoveltyArchive      what regions of behaviour space are still unexplored
  FailureArchive      what has already been tried and did not work

The failure archive is the one most often skipped and the one with the clearest
payoff on this route: at ~130 seconds per generation, re-deriving a mutation
that already failed is expensive. Retrieval is deliberately *selective* — the
spec warns against stuffing whole histories into prompts, and a prompt full of
failures crowds out the program being improved.
"""

from __future__ import annotations

import math
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


@dataclass
class Entry:
    """One archived candidate."""

    candidate_id: str
    metrics: Dict[str, float] = field(default_factory=dict)
    generation: Optional[int] = None
    island: Optional[int] = None
    operator: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    parent_id: Optional[str] = None
    code_hash: Optional[str] = None
    behaviour: Optional[Sequence[float]] = None
    timestamp: float = field(default_factory=time.time)
    note: str = ""

    def score(self, key: str = "combined_score") -> Optional[float]:
        v = self.metrics.get(key)
        return float(v) if isinstance(v, (int, float)) else None

    def to_dict(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        d["behaviour"] = list(self.behaviour) if self.behaviour is not None else None
        return d


class HallOfFame:
    """
    Every candidate that was ever champion, in order.

    Not the same as "top N by score": a candidate that held the crown for 200
    generations and was then beaten is historically important, and a plain
    top-N list silently drops it once N better candidates exist.
    """

    def __init__(self, metric: str = "combined_score", capacity: int = 200) -> None:
        self.metric = metric
        self.capacity = capacity
        self.entries: List[Entry] = []
        self._best: Optional[float] = None

    def consider(self, entry: Entry) -> bool:
        s = entry.score(self.metric)
        if s is None:
            return False
        if self._best is None or s > self._best:
            self._best = s
            self.entries.append(entry)
            if len(self.entries) > self.capacity:
                # Drop the oldest *middle* entries, never the first (the origin
                # of the lineage) or the most recent (the current champion).
                del self.entries[1:2]
            return True
        return False

    @property
    def champion(self) -> Optional[Entry]:
        return self.entries[-1] if self.entries else None

    def progression(self) -> List[Tuple[Optional[int], float]]:
        return [(e.generation, e.score(self.metric) or 0.0) for e in self.entries]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metric": self.metric,
            "size": len(self.entries),
            "champion": self.champion.to_dict() if self.champion else None,
            "progression": self.progression(),
        }


class ParetoArchive:
    """
    Non-dominated set over several objectives.

    Needed because a single combined score hides trade-offs: a candidate that is
    slightly less accurate but ten times faster is genuinely interesting, and
    scalarisation makes it invisible.

    `objectives` maps metric name → True to maximise, False to minimise.
    """

    def __init__(self, objectives: Dict[str, bool], capacity: int = 500) -> None:
        if not objectives:
            raise ValueError("at least one objective is required")
        self.objectives = dict(objectives)
        self.capacity = capacity
        self.entries: List[Entry] = []
        self.rejected = 0

    def _vector(self, e: Entry) -> Optional[List[float]]:
        out: List[float] = []
        for name, maximise in self.objectives.items():
            v = e.metrics.get(name)
            if not isinstance(v, (int, float)) or math.isnan(v):
                return None
            out.append(float(v) if maximise else -float(v))
        return out

    @staticmethod
    def _dominates(a: Sequence[float], b: Sequence[float]) -> bool:
        """`a` dominates `b`: at least as good everywhere, strictly better once."""
        return all(x >= y for x, y in zip(a, b)) and any(x > y for x, y in zip(a, b))

    def consider(self, entry: Entry) -> bool:
        v = self._vector(entry)
        if v is None:
            self.rejected += 1
            return False

        surviving: List[Entry] = []
        for existing in self.entries:
            ev = self._vector(existing)
            if ev is None:
                continue
            if self._dominates(ev, v):
                self.rejected += 1
                return False          # dominated by something already here
            if not self._dominates(v, ev):
                surviving.append(existing)   # keep whatever we do not dominate

        surviving.append(entry)
        self.entries = surviving
        if len(self.entries) > self.capacity:
            # Trim the most crowded region so the front stays evenly covered
            # rather than losing its extremes.
            self.entries.pop(self._most_crowded_index())
        return True

    def _most_crowded_index(self) -> int:
        vs = [self._vector(e) or [] for e in self.entries]
        best_i, best_d = 0, math.inf
        for i, vi in enumerate(vs):
            d = min(
                (sum((a - b) ** 2 for a, b in zip(vi, vj)) for j, vj in enumerate(vs)
                 if j != i and vj),
                default=math.inf,
            )
            if d < best_d:
                best_i, best_d = i, d
        return best_i

    def front(self) -> List[Entry]:
        return list(self.entries)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "objectives": self.objectives,
            "front_size": len(self.entries),
            "rejected": self.rejected,
            "front": [e.to_dict() for e in self.entries[:50]],
        }


class NoveltyArchive:
    """
    Behaviour-space coverage, by k-nearest-neighbour distance.

    Novelty is measured against what has been *seen*, not against the champion,
    which is what lets a search escape a local optimum that fitness alone would
    keep it in.
    """

    def __init__(self, k: int = 5, threshold: float = 0.0, capacity: int = 2000) -> None:
        self.k = k
        self.threshold = threshold
        self.capacity = capacity
        self.entries: List[Entry] = []

    @staticmethod
    def _distance(a: Sequence[float], b: Sequence[float]) -> float:
        return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))

    def novelty(self, behaviour: Sequence[float]) -> float:
        """Mean distance to the k nearest neighbours. Empty archive ⇒ maximal."""
        pts = [e.behaviour for e in self.entries if e.behaviour is not None]
        if not pts:
            return math.inf
        ds = sorted(self._distance(behaviour, p) for p in pts)
        take = ds[: min(self.k, len(ds))]
        return sum(take) / len(take)

    def consider(self, entry: Entry) -> Tuple[bool, float]:
        if entry.behaviour is None:
            return False, 0.0
        n = self.novelty(entry.behaviour)
        if n < self.threshold:
            return False, n
        self.entries.append(entry)
        if len(self.entries) > self.capacity:
            self.entries.pop(0)
        return True, n

    def to_dict(self) -> Dict[str, Any]:
        return {"k": self.k, "threshold": self.threshold, "size": len(self.entries)}


class FailureArchive:
    """
    What was tried and did not work, indexed for *selective* retrieval.

    Two uses, both concrete:
      * Prompt context — a short list of already-failed approaches, so an
        expensive request does not re-derive one.
      * Operator statistics — which operator classes produce which failures,
        which is signal the bandit's scalar reward cannot express.

    `recent_for_prompt` caps hard. A prompt stuffed with history crowds out the
    program being improved, and the spec warns against exactly that.
    """

    def __init__(self, capacity: int = 5000) -> None:
        self.capacity = capacity
        self.entries: List[Entry] = []
        self.by_reason: Counter = Counter()
        self.by_operator: Counter = Counter()
        self._seen_hashes: set = set()

    def record(self, entry: Entry, reason: str) -> None:
        entry.note = reason
        self.entries.append(entry)
        self.by_reason[reason] += 1
        if entry.operator:
            self.by_operator[entry.operator] += 1
        if entry.code_hash:
            self._seen_hashes.add(entry.code_hash)
        if len(self.entries) > self.capacity:
            self.entries.pop(0)

    def already_failed(self, code_hash: str) -> bool:
        """Cheap pre-check: has this exact program already failed?"""
        return code_hash in self._seen_hashes

    def recent_for_prompt(self, limit: int = 5, operator: Optional[str] = None) -> List[str]:
        pool = [e for e in reversed(self.entries)
                if operator is None or e.operator == operator]
        out: List[str] = []
        seen: set = set()
        for e in pool:
            # De-duplicate by reason: five copies of the same failure teach the
            # model nothing extra and cost five lines of context.
            if e.note in seen:
                continue
            seen.add(e.note)
            out.append(f"- {e.note}" + (f" (operator: {e.operator})" if e.operator else ""))
            if len(out) >= limit:
                break
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "size": len(self.entries),
            "unique_failed_programs": len(self._seen_hashes),
            "by_reason": dict(self.by_reason.most_common(20)),
            "by_operator": dict(self.by_operator.most_common(20)),
        }


class ArchiveSet:
    """All four archives, fed from one place so they cannot drift apart."""

    def __init__(
        self,
        *,
        metric: str = "combined_score",
        objectives: Optional[Dict[str, bool]] = None,
        novelty_k: int = 5,
    ) -> None:
        self.hall_of_fame = HallOfFame(metric=metric)
        self.pareto = ParetoArchive(objectives or {metric: True})
        self.novelty = NoveltyArchive(k=novelty_k)
        self.failures = FailureArchive()
        self.accepted = 0

    def accept(self, entry: Entry) -> Dict[str, Any]:
        self.accepted += 1
        new_champion = self.hall_of_fame.consider(entry)
        on_front = self.pareto.consider(entry)
        novel, novelty = self.novelty.consider(entry)
        return {
            "new_champion": new_champion,
            "pareto_front": on_front,
            "novel": novel,
            "novelty": None if math.isinf(novelty) else round(novelty, 5),
        }

    def reject(self, entry: Entry, reason: str) -> None:
        self.failures.record(entry, reason)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "accepted": self.accepted,
            "hall_of_fame": self.hall_of_fame.to_dict(),
            "pareto": self.pareto.to_dict(),
            "novelty": self.novelty.to_dict(),
            "failures": self.failures.to_dict(),
        }
