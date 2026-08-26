"""
Per-route mutation quality.

`Router.stats_by_route` answers "does this route respond?" — success rate,
latency, tokens. That is reliability, and it is not the question T1 asks.

The question T1 asks is "does this route produce *better mutations*, and at what
cost?" A route can be 100% reliable and produce nothing but duplicates and
syntax errors. Live measurement already showed the two questions come apart:

    x-preview-f-free (Ox Alpha)   41% reliable   229 s/request
    nemotron-3-ultra-free        100% reliable   112 s/request

Reliability alone would say "switch". That would be a mistake without knowing
whether Ox Alpha's slower, flakier requests carry better mutations — which is
exactly what this module measures.

Three efficiency views, because "best" depends on what is scarce:

  per request   what matters under a provider rate contract (NIM: 48 RPM)
  per second    what matters when wall-clock is the constraint
  per token     what matters when a paid provider is billing you

The same two routes can rank differently under each, and that is a real result
rather than a contradiction.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional


@dataclass
class Attempt:
    """One mutation attempt, attributed to the route that generated it."""

    route: str                       # "provider/model"
    operator: Optional[str] = None   # OperatorClass, when known
    # Outcome of the cheap gates, before anything expensive ran.
    failed: bool = False             # the request itself never returned usable text
    parsed: bool = True              # produced an applicable diff at all
    passed_g0: bool = True           # valid program
    passed_g1: bool = True           # not a duplicate
    accepted: bool = False           # entered the population
    fitness_delta: Optional[float] = None   # vs parent; None if unscored
    latency_ms: float = 0.0
    tokens: int = 0
    reasoning_tokens: int = 0
    timestamp: float = field(default_factory=time.time)

    @property
    def useful(self) -> bool:
        """
        A candidate that reached the population and was not a duplicate.

        Deliberately not "improved the champion": in a quality-diversity search
        a candidate that widens the archive has value even when it loses to the
        incumbent. Counting only improvements would make every route look
        useless late in a run, when improvements are rare by construction.
        """
        return self.accepted and self.passed_g1

    @property
    def improved(self) -> bool:
        return self.accepted and (self.fitness_delta or 0.0) > 0


@dataclass
class RouteStats:
    route: str
    attempts: int = 0
    failures: int = 0
    unparseable: int = 0
    g0_failures: int = 0
    duplicates: int = 0
    accepted: int = 0
    improvements: int = 0
    total_latency_ms: float = 0.0
    total_tokens: int = 0
    total_reasoning_tokens: int = 0
    total_positive_delta: float = 0.0
    best_delta: Optional[float] = None

    # -- rates ---------------------------------------------------------

    @property
    def parse_rate(self) -> float:
        return 1.0 - (self.unparseable / self.attempts) if self.attempts else 0.0

    @property
    def failure_rate(self) -> float:
        """
        Requests that never returned usable text — timeouts, 5xx, truncation
        the broker could not rescue.

        Kept separate from `unparseable` on purpose: a 200 response whose diff
        does not apply is a *model* problem, while a timeout is a *route*
        problem, and the fix for each is different. They are both charged as
        attempts, though, because both consumed the wall-clock and the rate
        budget that the efficiency measures divide by.
        """
        return self.failures / self.attempts if self.attempts else 0.0

    @property
    def validity_rate(self) -> float:
        """Fraction of attempts that produced a valid, novel program."""
        return self.accepted / self.attempts if self.attempts else 0.0

    @property
    def duplicate_rate(self) -> float:
        return self.duplicates / self.attempts if self.attempts else 0.0

    @property
    def improvement_rate(self) -> float:
        return self.improvements / self.attempts if self.attempts else 0.0

    @property
    def mean_latency_s(self) -> Optional[float]:
        return (self.total_latency_ms / self.attempts / 1000.0) if self.attempts else None

    @property
    def reasoning_share(self) -> Optional[float]:
        """How much of the completion budget went to hidden reasoning."""
        if not self.total_tokens:
            return None
        return self.total_reasoning_tokens / self.total_tokens

    # -- efficiency ----------------------------------------------------

    @property
    def improvement_per_request(self) -> float:
        return self.total_positive_delta / self.attempts if self.attempts else 0.0

    @property
    def improvement_per_second(self) -> Optional[float]:
        secs = self.total_latency_ms / 1000.0
        return self.total_positive_delta / secs if secs > 0 else None

    @property
    def improvement_per_1k_tokens(self) -> Optional[float]:
        return (self.total_positive_delta / (self.total_tokens / 1000.0)
                if self.total_tokens else None)

    def record(self, a: Attempt) -> None:
        self.attempts += 1
        self.total_latency_ms += a.latency_ms
        self.total_tokens += a.tokens
        self.total_reasoning_tokens += a.reasoning_tokens
        if a.failed:
            self.failures += 1
            return
        if not a.parsed:
            self.unparseable += 1
            return
        if not a.passed_g0:
            self.g0_failures += 1
            return
        if not a.passed_g1:
            self.duplicates += 1
            return
        if a.accepted:
            self.accepted += 1
        d = a.fitness_delta
        if d is not None and d > 0:
            self.improvements += 1
            self.total_positive_delta += d
            self.best_delta = d if self.best_delta is None else max(self.best_delta, d)

    def to_dict(self) -> Dict[str, Any]:
        def r(v, n=4):
            return None if v is None else round(v, n)
        return {
            "route": self.route,
            "attempts": self.attempts,
            "accepted": self.accepted,
            "improvements": self.improvements,
            "failures": self.failures,
            "unparseable": self.unparseable,
            "g0_failures": self.g0_failures,
            "duplicates": self.duplicates,
            "parse_rate": r(self.parse_rate),
            "failure_rate": r(self.failure_rate),
            "validity_rate": r(self.validity_rate),
            "duplicate_rate": r(self.duplicate_rate),
            "improvement_rate": r(self.improvement_rate),
            "mean_latency_s": r(self.mean_latency_s, 1),
            "reasoning_share": r(self.reasoning_share),
            "total_tokens": self.total_tokens,
            "best_delta": r(self.best_delta, 5),
            "improvement_per_request": r(self.improvement_per_request, 5),
            "improvement_per_second": r(self.improvement_per_second, 7),
            "improvement_per_1k_tokens": r(self.improvement_per_1k_tokens, 5),
        }


# How many attempts a route needs before its numbers are worth acting on.
# Not a statistical test — a guard against the obvious failure of switching the
# operator's chosen primary on the strength of three samples.
MIN_ATTEMPTS_FOR_COMPARISON = 20


class RouteQualityTracker:
    """Accumulates per-route mutation quality and ranks routes by efficiency."""

    def __init__(self, min_attempts: int = MIN_ATTEMPTS_FOR_COMPARISON) -> None:
        self.routes: Dict[str, RouteStats] = {}
        self.by_operator: Dict[str, Dict[str, RouteStats]] = {}
        self.min_attempts = min_attempts

    def record(self, a: Attempt) -> None:
        self.routes.setdefault(a.route, RouteStats(a.route)).record(a)
        if a.operator:
            self.by_operator.setdefault(a.operator, {}).setdefault(
                a.route, RouteStats(a.route)).record(a)

    def rank(self, by: str = "improvement_per_second") -> List[RouteStats]:
        """
        Rank routes by one efficiency measure, best first.

        Routes below `min_attempts` are excluded rather than ranked low: a route
        with two lucky samples should not top the table, and one with two
        unlucky samples should not be condemned.
        """
        eligible = [s for s in self.routes.values() if s.attempts >= self.min_attempts]

        def key(s: RouteStats) -> float:
            v = getattr(s, by, None)
            return v if isinstance(v, (int, float)) else -math.inf

        return sorted(eligible, key=key, reverse=True)

    def compare(self) -> Dict[str, Any]:
        """
        The answer T1 needs, with the caveats attached rather than implied.

        Returns the ranking under each scarcity model, plus an explicit
        `verdict` that says when the evidence is too thin to act on — because
        the failure mode here is a confident recommendation from noise.
        """
        views = {
            m: [s.to_dict() for s in self.rank(m)]
            for m in ("improvement_per_request", "improvement_per_second",
                      "improvement_per_1k_tokens", "validity_rate")
        }
        eligible = self.rank("improvement_per_second")
        excluded = {
            s.route: f"only {s.attempts} attempts (need {self.min_attempts})"
            for s in self.routes.values() if s.attempts < self.min_attempts
        }

        verdict: str
        if len(eligible) < 2:
            verdict = (
                f"insufficient evidence: {len(eligible)} route(s) have at least "
                f"{self.min_attempts} attempts. Do not change routing on this."
            )
        else:
            best_s, second_s = eligible[0], eligible[1]
            a = best_s.improvement_per_second or 0.0
            b = second_s.improvement_per_second or 0.0
            if a <= 0 and b <= 0:
                verdict = ("no route produced a measurable improvement yet; "
                           "nothing to compare.")
            elif b > 0 and a / b < 1.25:
                # Within noise for a sample this size.
                verdict = (
                    f"{best_s.route} leads {second_s.route} by only "
                    f"{a / b:.2f}x on improvement/second — too close to call at "
                    f"this sample size. Gather more attempts before switching."
                )
            else:
                verdict = (
                    f"{best_s.route} leads on improvement/second "
                    f"({a:.2e} vs {b:.2e}). Worth proposing a routing change — "
                    f"but the primary route is an operator preference, so "
                    f"present the evidence rather than switching silently."
                )

        return {
            "views": views,
            "excluded_insufficient_data": excluded,
            "min_attempts": self.min_attempts,
            "verdict": verdict,
        }

    def operator_breakdown(self) -> Dict[str, List[Dict[str, Any]]]:
        """
        Per-operator, per-route quality.

        This is where a nuanced answer can appear: a slow, strong model may earn
        its latency on RADICAL_RETHINK and waste it on PARAMETER_CHANGE, which
        argues for routing *by operator* rather than picking one winner.
        """
        return {
            op: [s.to_dict() for s in sorted(
                routes.values(), key=lambda s: s.improvement_per_request, reverse=True)]
            for op, routes in self.by_operator.items()
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "routes": {k: v.to_dict() for k, v in self.routes.items()},
            "comparison": self.compare(),
            "by_operator": self.operator_breakdown(),
        }

    def render(self) -> str:
        """Plain-text table for the terminal dashboard."""
        rows = self.rank("improvement_per_request") or list(self.routes.values())
        if not rows:
            return "no mutation attempts recorded yet"
        out = [
            "{:<34}{:>6}{:>8}{:>8}{:>9}{:>11}{:>12}".format(
                "route", "n", "valid", "dup", "improv", "mean s", "impr/req"),
            "-" * 88,
        ]
        for s in rows:
            out.append("{:<34}{:>6}{:>7.0%}{:>8.0%}{:>9.0%}{:>11}{:>12}".format(
                s.route[:33], s.attempts, s.validity_rate, s.duplicate_rate,
                s.improvement_rate,
                f"{s.mean_latency_s:.0f}" if s.mean_latency_s else "-",
                f"{s.improvement_per_request:.4f}",
            ))
        return "\n".join(out)

    # -- persistence ---------------------------------------------------

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({k: v.__dict__ for k, v in self.routes.items()}, fh, indent=2)

    @classmethod
    def load(cls, path: str) -> "RouteQualityTracker":
        t = cls()
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for route, d in json.load(fh).items():
                    s = RouteStats(route)
                    for k, v in d.items():
                        if hasattr(s, k):
                            setattr(s, k, v)
                    t.routes[route] = s
        except (OSError, ValueError):
            # A missing or corrupt file means "no history", not an error —
            # quality data is an optimisation input, never a correctness input.
            pass
        return t
