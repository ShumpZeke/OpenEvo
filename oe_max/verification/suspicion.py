"""
Which candidates are worth verifying.

Verification costs real time, so running it on everything would trade the
throughput the rest of the system exists to buy. Running it on nothing means
the first candidate that games the metric becomes the champion and every
subsequent generation is built on it.

The compromise is to spend it where the evidence is odd: a score that jumps
much further than this run's own history says a jump should. That is exactly
the shape of a candidate that stopped solving the problem and started reporting
a number.

Robust statistics, not mean and standard deviation
--------------------------------------------------

The thing being detected is an outlier, and an outlier drags the mean and
inflates the standard deviation — so a z-score test is *desensitised by the
very event it is meant to catch*, and the second cheat sails through. Median
and median-absolute-deviation are not moved by a single extreme value, which is
why they are used here despite being less familiar.

A floor on the MAD matters just as much: on a plateau, where every recent
improvement is ~0, the MAD collapses toward zero and any nonzero jump looks
infinitely suspicious. Without the floor this flags every candidate late in a
run, which is where a run spends most of its time.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Below this many observed improvements there is no distribution to compare
# against, and "unusual" is not yet a meaningful word.
MIN_HISTORY = 8

# How many MADs above the median counts as suspicious. 3.5 is the common
# convention for the modified z-score; it is a threshold, not a discovery.
DEFAULT_THRESHOLD = 3.5

# The smallest jump scale to reason about. Prevents a plateau — where recent
# improvements are all ~0 — from making every candidate an outlier.
MIN_SCALE = 1e-6

# 0.6745 is the 75th percentile of the standard normal: it rescales the MAD so
# the threshold means roughly what it would with a standard deviation.
_MAD_TO_SIGMA = 0.6745


@dataclass
class SuspicionVerdict:
    suspicious: bool
    score: Optional[float]           # modified z-score of this jump
    reason: str
    delta: Optional[float] = None
    median_delta: Optional[float] = None
    scale: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        def r(v):
            return None if v is None else round(v, 6)
        return {"suspicious": self.suspicious, "score": r(self.score),
                "reason": self.reason, "delta": r(self.delta),
                "median_delta": r(self.median_delta), "scale": r(self.scale)}


@dataclass
class SuspicionDetector:
    """Tracks the distribution of improvements and flags the ones that do not fit."""

    threshold: float = DEFAULT_THRESHOLD
    min_history: int = MIN_HISTORY
    window: int = 200
    deltas: List[float] = field(default_factory=list)

    def observe(self, delta: float) -> None:
        """Record an improvement. Only positive jumps shape the distribution."""
        if delta is None or delta <= 0:
            return
        self.deltas.append(float(delta))
        if len(self.deltas) > self.window:
            del self.deltas[: len(self.deltas) - self.window]

    def check(self, delta: Optional[float], *, observe: bool = True) -> SuspicionVerdict:
        """
        Judge one improvement against the run's own history.

        The history is this run's, not a global constant, because what counts
        as a big jump differs by task, by metric scale and by how far into the
        run it is.
        """
        if delta is None:
            return SuspicionVerdict(False, None, "no score change to judge")
        delta = float(delta)
        if delta <= 0:
            if observe:
                self.observe(delta)
            return SuspicionVerdict(False, None, "not an improvement")

        if len(self.deltas) < self.min_history:
            verdict = SuspicionVerdict(
                False, None,
                f"only {len(self.deltas)} prior improvements "
                f"(need {self.min_history}); not enough history to call it unusual")
            if observe:
                self.observe(delta)
            return verdict

        median = statistics.median(self.deltas)
        mad = statistics.median([abs(d - median) for d in self.deltas])
        scale = max(mad / _MAD_TO_SIGMA, MIN_SCALE)
        score = (delta - median) / scale

        suspicious = score > self.threshold
        reason = (
            f"jump {delta:.6g} is {score:.1f}x the typical scale above the "
            f"median improvement {median:.6g} — verify before trusting it"
            if suspicious else
            f"jump {delta:.6g} is within {self.threshold} of the median "
            f"improvement {median:.6g}"
        )
        # Observed either way, and deliberately: excluding flagged jumps would
        # make a genuine breakthrough permanently suspicious, and every real
        # improvement after it too.
        if observe:
            self.observe(delta)
        return SuspicionVerdict(suspicious, score, reason, delta, median, scale)

    def snapshot(self) -> Dict[str, Any]:
        if not self.deltas:
            return {"observations": 0, "median_delta": None, "scale": None}
        median = statistics.median(self.deltas)
        mad = statistics.median([abs(d - median) for d in self.deltas])
        return {
            "observations": len(self.deltas),
            "median_delta": round(median, 6),
            "scale": round(max(mad / _MAD_TO_SIGMA, MIN_SCALE), 6),
            "threshold": self.threshold,
        }
