"""
Adaptive operator selection.

The spec is specific: use cheap statistics rather than another model call, start
with a non-stationary bandit such as discounted Thompson sampling, and keep the
algorithm replaceable.

Non-stationarity is the whole point. Which operator pays off changes as the run
progresses — STRUCTURAL_REWRITE tends to earn its keep early, PARAMETER_CHANGE
on a plateau. A stationary bandit averages over the entire run and keeps
selecting whatever won in the first hundred trials. Discounting decays old
evidence so the selector can change its mind.

`Selector` is the replaceable interface. `UniformRandom` and `EpsilonGreedy`
exist so the "no operator bandit" ablation the spec requires is a one-line
substitution rather than a code removal.
"""

from __future__ import annotations

import math
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Hashable, List, Optional, Sequence


@dataclass
class ArmStats:
    alpha: float = 1.0          # Beta prior: pseudo-successes
    beta: float = 1.0           # Beta prior: pseudo-failures
    pulls: int = 0
    total_reward: float = 0.0
    last_reward: Optional[float] = None

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def observed_mean(self) -> Optional[float]:
        return self.total_reward / self.pulls if self.pulls else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pulls": self.pulls,
            "posterior_mean": round(self.mean, 4),
            "observed_mean": (round(self.observed_mean, 4)
                              if self.observed_mean is not None else None),
            "alpha": round(self.alpha, 3),
            "beta": round(self.beta, 3),
            "last_reward": self.last_reward,
        }


class Selector(ABC):
    """Replaceable selection strategy over a fixed arm set."""

    name = "selector"

    def __init__(self, arms: Sequence[Hashable], *, seed: Optional[int] = None) -> None:
        self.arms: List[Hashable] = list(arms)
        self.stats: Dict[Hashable, ArmStats] = {a: ArmStats() for a in self.arms}
        self._rng = random.Random(seed)

    def ensure_arm(self, arm: Hashable) -> None:
        if arm not in self.stats:
            self.arms.append(arm)
            self.stats[arm] = ArmStats()

    @abstractmethod
    def select(self, candidates: Optional[Sequence[Hashable]] = None) -> Hashable:
        ...

    @abstractmethod
    def update(self, arm: Hashable, reward: float) -> None:
        ...

    def _pool(self, candidates: Optional[Sequence[Hashable]]) -> List[Hashable]:
        pool = [a for a in (candidates if candidates is not None else self.arms)
                if a in self.stats]
        if not pool:
            raise ValueError("no eligible arms to select from")
        return pool

    def snapshot(self) -> Dict[str, Any]:
        ranked = sorted(self.stats.items(), key=lambda kv: kv[1].mean, reverse=True)
        return {
            "selector": self.name,
            "arms": {str(a): s.to_dict() for a, s in self.stats.items()},
            "ranking": [str(a) for a, _ in ranked],
            "total_pulls": sum(s.pulls for s in self.stats.values()),
        }


class DiscountedThompsonSampling(Selector):
    """
    Thompson sampling over Beta posteriors with geometric discounting.

    Each update decays *every* arm's evidence by `gamma` toward the prior, so
    information has a half-life of roughly `ln(0.5)/ln(gamma)` updates. At the
    default gamma=0.95 that is ~13 updates — long enough to be stable, short
    enough to track a phase change within a run.

    Rewards must be in [0, 1]. Callers normalise: a useful convention is
    fitness-delta clipped to [0,1], with 0 for a rejected or invalid candidate,
    so an operator that mostly produces garbage is penalised naturally.
    """

    name = "discounted_thompson"

    def __init__(self, arms: Sequence[Hashable], *, gamma: float = 0.95,
                 prior_alpha: float = 1.0, prior_beta: float = 1.0,
                 seed: Optional[int] = None) -> None:
        super().__init__(arms, seed=seed)
        if not 0.0 < gamma <= 1.0:
            raise ValueError("gamma must be in (0, 1]")
        self.gamma = gamma
        self.prior_alpha = prior_alpha
        self.prior_beta = prior_beta
        for s in self.stats.values():
            s.alpha, s.beta = prior_alpha, prior_beta

    def ensure_arm(self, arm: Hashable) -> None:
        super().ensure_arm(arm)
        s = self.stats[arm]
        if s.pulls == 0:
            s.alpha, s.beta = self.prior_alpha, self.prior_beta

    def select(self, candidates: Optional[Sequence[Hashable]] = None) -> Hashable:
        pool = self._pool(candidates)
        best, best_sample = pool[0], -1.0
        for arm in pool:
            s = self.stats[arm]
            sample = self._rng.betavariate(max(s.alpha, 1e-6), max(s.beta, 1e-6))
            if sample > best_sample:
                best, best_sample = arm, sample
        return best

    def update(self, arm: Hashable, reward: float) -> None:
        self.ensure_arm(arm)
        reward = min(1.0, max(0.0, float(reward)))

        # Decay every arm, not just the pulled one: the passage of time is what
        # makes old evidence stale, and only decaying the pulled arm would make
        # rarely-pulled arms look permanently authoritative.
        for s in self.stats.values():
            s.alpha = self.prior_alpha + self.gamma * (s.alpha - self.prior_alpha)
            s.beta = self.prior_beta + self.gamma * (s.beta - self.prior_beta)

        s = self.stats[arm]
        s.alpha += reward
        s.beta += 1.0 - reward
        s.pulls += 1
        s.total_reward += reward
        s.last_reward = reward

    @property
    def evidence_half_life(self) -> float:
        return math.inf if self.gamma >= 1.0 else math.log(0.5) / math.log(self.gamma)


class EpsilonGreedy(Selector):
    """Simpler baseline, for comparison and ablation."""

    name = "epsilon_greedy"

    def __init__(self, arms: Sequence[Hashable], *, epsilon: float = 0.1,
                 seed: Optional[int] = None) -> None:
        super().__init__(arms, seed=seed)
        self.epsilon = epsilon

    def select(self, candidates: Optional[Sequence[Hashable]] = None) -> Hashable:
        pool = self._pool(candidates)
        if self._rng.random() < self.epsilon:
            return self._rng.choice(pool)
        unpulled = [a for a in pool if self.stats[a].pulls == 0]
        if unpulled:
            return self._rng.choice(unpulled)
        return max(pool, key=lambda a: self.stats[a].observed_mean or 0.0)

    def update(self, arm: Hashable, reward: float) -> None:
        self.ensure_arm(arm)
        reward = min(1.0, max(0.0, float(reward)))
        s = self.stats[arm]
        s.pulls += 1
        s.total_reward += reward
        s.last_reward = reward
        s.alpha += reward
        s.beta += 1.0 - reward


class UniformRandom(Selector):
    """The "no operator bandit" ablation: pick uniformly, still record stats."""

    name = "uniform_random"

    def select(self, candidates: Optional[Sequence[Hashable]] = None) -> Hashable:
        return self._rng.choice(self._pool(candidates))

    def update(self, arm: Hashable, reward: float) -> None:
        self.ensure_arm(arm)
        reward = min(1.0, max(0.0, float(reward)))
        s = self.stats[arm]
        s.pulls += 1
        s.total_reward += reward
        s.last_reward = reward


SELECTORS = {
    "discounted_thompson": DiscountedThompsonSampling,
    "epsilon_greedy": EpsilonGreedy,
    "uniform_random": UniformRandom,
}


def build_selector(name: str, arms: Sequence[Hashable], **kw: Any) -> Selector:
    if name not in SELECTORS:
        raise ValueError(f"unknown selector {name!r}; have {sorted(SELECTORS)}")
    return SELECTORS[name](arms, **kw)


def reward_from_outcome(
    *, accepted: bool, fitness_delta: Optional[float] = None,
    scale: float = 0.05,
) -> float:
    """
    Map an evaluation outcome to a reward in [0, 1].

    Rejected or invalid candidates score 0, so an operator that mostly emits
    unparseable or duplicate programs is penalised without needing a separate
    validity signal. Accepted-but-not-better scores a small positive value:
    a candidate that widens the archive without beating the champion still has
    value in a quality-diversity search, and scoring it zero would push the
    selector toward pure exploitation.
    """
    if not accepted:
        return 0.0
    if fitness_delta is None:
        return 0.5
    if fitness_delta <= 0:
        return 0.25
    # Saturating: one enormous jump should not dominate the posterior forever,
    # and outliers are exactly what the anti-reward-hacking checks distrust.
    return min(1.0, 0.5 + 0.5 * (1.0 - math.exp(-fitness_delta / max(scale, 1e-9))))
