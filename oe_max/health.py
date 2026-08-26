"""
Provider health, retry policy and circuit breaking.

Separated from the rate limiter on purpose. They answer different questions:

  limiter  "may I start an attempt right now?"   — a contract with the provider
  health   "is this route worth attempting?"     — an observation about it

Conflating them produces a limiter that stops enforcing its bound when a
provider looks unhealthy, which is exactly when retries spike and the bound
matters most.
"""

from __future__ import annotations

import random
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Deque, Dict, Optional, Tuple

from .providers.base import Outcome


class CircuitState(str, Enum):
    CLOSED = "closed"        # healthy, traffic flows
    OPEN = "open"            # failing, traffic blocked
    HALF_OPEN = "half_open"  # cooldown elapsed, allowing one probe


@dataclass
class RetryPolicy:
    max_attempts: int = 4
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0
    jitter: bool = True
    honor_retry_after: bool = True

    def delay_for(self, attempt: int, retry_after: Optional[float] = None) -> float:
        """Exponential backoff with jitter; provider guidance wins when given."""
        if self.honor_retry_after and retry_after is not None:
            base = max(retry_after, 0.0)
        else:
            base = min(self.base_delay_s * (2 ** max(0, attempt - 1)), self.max_delay_s)
        if self.jitter:
            # Full jitter: decorrelates retries so N workers that failed together
            # do not all retry together and re-create the burst.
            base = random.uniform(0.0, base) if base > 0 else 0.0
        return min(base, self.max_delay_s)


@dataclass
class HealthWindow:
    """Rolling health for one provider/model route."""

    size: int = 50
    outcomes: Deque[bool] = field(default_factory=lambda: deque(maxlen=50))
    latencies: Deque[float] = field(default_factory=lambda: deque(maxlen=50))
    consecutive_failures: int = 0
    total_attempts: int = 0
    total_failures: int = 0
    total_rate_limited: int = 0
    total_tokens: int = 0
    last_error: Optional[str] = None
    last_used: float = 0.0

    def record(self, ok: bool, latency_ms: Optional[float] = None,
               rate_limited: bool = False, tokens: int = 0,
               error: Optional[str] = None) -> None:
        self.outcomes.append(ok)
        self.total_attempts += 1
        self.last_used = time.time()
        if latency_ms is not None:
            self.latencies.append(latency_ms)
        if rate_limited:
            self.total_rate_limited += 1
        if tokens:
            self.total_tokens += tokens
        if ok:
            self.consecutive_failures = 0
        else:
            self.consecutive_failures += 1
            self.total_failures += 1
            self.last_error = error

    @property
    def success_rate(self) -> float:
        # An unused route is optimistic, so a fresh fallback can win a route and
        # prove itself instead of being locked out for having no history.
        return sum(self.outcomes) / len(self.outcomes) if self.outcomes else 1.0

    @property
    def p50_latency_ms(self) -> Optional[float]:
        if not self.latencies:
            return None
        s = sorted(self.latencies)
        return s[len(s) // 2]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success_rate": round(self.success_rate, 4),
            "p50_latency_ms": self.p50_latency_ms,
            "consecutive_failures": self.consecutive_failures,
            "total_attempts": self.total_attempts,
            "total_failures": self.total_failures,
            "total_rate_limited": self.total_rate_limited,
            "total_tokens": self.total_tokens,
            "last_error": self.last_error,
            "last_used": self.last_used or None,
        }


class CircuitBreaker:
    """
    Standard three-state breaker.

    Half-open admits exactly one probe: if a provider is down, letting the full
    load back in to "test" it just re-fails everything and restarts the cooldown.
    """

    def __init__(self, failure_threshold: int = 5, window_seconds: float = 60.0,
                 cooldown_seconds: float = 45.0) -> None:
        self.failure_threshold = failure_threshold
        self.window_seconds = window_seconds
        self.cooldown_seconds = cooldown_seconds
        self._failures: Deque[float] = deque()
        self._opened_at: Optional[float] = None
        self._half_open_in_flight = False
        self.trips = 0

    def state(self, now: Optional[float] = None) -> CircuitState:
        now = now if now is not None else time.time()
        if self._opened_at is None:
            return CircuitState.CLOSED
        if now - self._opened_at >= self.cooldown_seconds:
            return CircuitState.HALF_OPEN
        return CircuitState.OPEN

    def allow(self, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.time()
        st = self.state(now)
        if st is CircuitState.CLOSED:
            return True
        if st is CircuitState.OPEN:
            return False
        if self._half_open_in_flight:
            return False
        self._half_open_in_flight = True
        return True

    def record_success(self, now: Optional[float] = None) -> None:
        self._failures.clear()
        self._opened_at = None
        self._half_open_in_flight = False

    def record_failure(self, now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        self._half_open_in_flight = False
        if self.state(now) is CircuitState.HALF_OPEN:
            # The probe failed: restart the full cooldown rather than
            # immediately re-probing.
            self._opened_at = now
            return
        self._failures.append(now)
        cutoff = now - self.window_seconds
        while self._failures and self._failures[0] <= cutoff:
            self._failures.popleft()
        if len(self._failures) >= self.failure_threshold:
            self._opened_at = now
            self.trips += 1
            self._failures.clear()

    def reset(self) -> None:
        self._failures.clear()
        self._opened_at = None
        self._half_open_in_flight = False

    def to_dict(self) -> Dict[str, Any]:
        now = time.time()
        st = self.state(now)
        return {
            "state": st.value,
            "recent_failures": len(self._failures),
            "failure_threshold": self.failure_threshold,
            "trips": self.trips,
            "cooldown_remaining_s": (
                round(max(0.0, self.cooldown_seconds - (now - self._opened_at)), 1)
                if self._opened_at and st is CircuitState.OPEN else 0.0
            ),
        }


# A route succeeding less than this, over at least this many attempts, is
# demoted out of the chain.
#
# Measured, not invented: Ox Alpha was observed at 8% success over 12 attempts
# while the circuit breaker sat closed with one recent failure.
#
# The reason is precise and worth knowing. The breaker trips on N failures
# inside a rolling **60-second** window, and Ox Alpha's requests take ~300
# seconds — so every failure ages out of the window before the next one
# arrives, and the count can never reach the threshold. A breaker whose window
# is shorter than the request latency is inert, and it is inert for exactly the
# slowest routes, where a wasted request costs the most.
#
# This check is latency-independent because its window counts *attempts*, not
# seconds.
DEGRADED_SUCCESS_RATE = 0.25
DEGRADED_MIN_ATTEMPTS = 10

# How long a route stays parked after the provider says its free allowance is
# spent. Free pools are typically restored on an hourly or daily boundary that
# the API does not disclose, so this is a compromise, not a derivation: long
# enough that we stop paying a request per call to be told the same thing,
# short enough that a refill is picked up within the same session. An operator
# who knows better can reset the route, which un-parks it immediately.
FREE_LIMIT_PARK_SECONDS = 900.0


class RouteHealth:
    """Health + breaker for every provider/model route the broker has used."""

    def __init__(self, failure_threshold: int = 5, cooldown_seconds: float = 45.0,
                 degraded_rate: float = DEGRADED_SUCCESS_RATE,
                 degraded_min_attempts: int = DEGRADED_MIN_ATTEMPTS,
                 free_limit_park_seconds: float = FREE_LIMIT_PARK_SECONDS) -> None:
        self._windows: Dict[str, HealthWindow] = {}
        self._breakers: Dict[str, CircuitBreaker] = {}
        self._parked: Dict[str, Tuple[float, str]] = {}
        self._failure_threshold = failure_threshold
        self._cooldown = cooldown_seconds
        self._degraded_rate = degraded_rate
        self._degraded_min_attempts = degraded_min_attempts
        self._free_limit_park = free_limit_park_seconds

    @staticmethod
    def key(provider: str, model: str) -> str:
        return f"{provider}/{model}"

    def window(self, provider: str, model: str) -> HealthWindow:
        return self._windows.setdefault(self.key(provider, model), HealthWindow())

    def breaker(self, provider: str, model: str) -> CircuitBreaker:
        return self._breakers.setdefault(
            self.key(provider, model),
            CircuitBreaker(failure_threshold=self._failure_threshold,
                           cooldown_seconds=self._cooldown),
        )

    def allow(self, provider: str, model: str) -> bool:
        if self.parked(provider, model):
            return False
        return self.breaker(provider, model).allow()

    # -- parking -------------------------------------------------------

    def park(self, provider: str, model: str, seconds: float,
             reason: str, *, now: Optional[float] = None) -> None:
        """
        Take a route out of service for a fixed period.

        Separate from both the breaker and `degraded`, because it answers a
        different question. The breaker asks "is this provider up?"; `degraded`
        asks "is this route mostly wasting our requests?". Parking is for a
        route that has told us *in words* that it will not serve us again for a
        while — an exhausted free allowance being the case that motivated it.
        Neither of the others can express that: the breaker would re-probe on a
        45-second cooldown and get the same refusal, and `degraded` needs ten
        attempts to notice something the provider stated on the first one.
        """
        now = now if now is not None else time.time()
        self._parked[self.key(provider, model)] = (now + max(0.0, seconds), reason)

    def parked(self, provider: str, model: str,
               *, now: Optional[float] = None) -> Optional[str]:
        """Why this route is parked, or None once the period has elapsed."""
        entry = self._parked.get(self.key(provider, model))
        if entry is None:
            return None
        until, reason = entry
        now = now if now is not None else time.time()
        if now >= until:
            # Expire lazily. A parked route that nothing asks about costs
            # nothing, and sweeping on a timer would need a timer.
            self._parked.pop(self.key(provider, model), None)
            return None
        return f"{reason} — {round(until - now)}s remaining"

    def degraded(self, provider: str, model: str) -> Optional[str]:
        """
        Why this route should be demoted, or None.

        Distinct from the breaker on purpose. The breaker is for an outage: N
        failures inside a time window, then a cooldown, then a half-open probe.
        This is for a route that merely mostly fails — where the failures are
        spread too thinly in time for the breaker to see them, or an occasional
        success clears its window, while nine of every ten requests are wasted.

        No cooldown, because the rolling window *is* the memory: the route is
        re-admitted the moment its recent attempts recover, without needing a
        timer to expire.
        """
        w = self.window(provider, model)
        if len(w.outcomes) < self._degraded_min_attempts:
            # An optimistic default is deliberate elsewhere too: a fresh route
            # deserves the chance to prove itself rather than being locked out
            # for having no history.
            return None
        rate = w.success_rate
        if rate >= self._degraded_rate:
            return None
        return (f"degraded: succeeding {rate:.0%} of the last "
                f"{len(w.outcomes)} attempts (needs {self._degraded_rate:.0%})")

    def success_rate(self, provider: str, model: str) -> float:
        return self.window(provider, model).success_rate

    def record(self, result: Any) -> None:
        """Fold one ChatResult into health and breaker state."""
        w = self.window(result.provider, result.model)
        b = self.breaker(result.provider, result.model)
        ok = result.outcome is Outcome.OK
        w.record(
            ok, result.latency_ms,
            rate_limited=result.outcome is Outcome.RATE_LIMITED,
            tokens=int((result.usage or {}).get("total_tokens") or 0),
            error=result.error,
        )
        if result.outcome is Outcome.FREE_LIMIT_EXHAUSTED:
            # The provider has stated the allowance is spent. Park the route
            # rather than letting the breaker treat it as an outage: it is not
            # down, it is closed to us, and re-probing it on a 45-second
            # cooldown just collects the same refusal.
            self.park(result.provider, result.model, self._free_limit_park,
                      "free allowance exhausted")

        if ok:
            b.record_success()
        elif result.outcome in (Outcome.BAD_REQUEST, Outcome.FREE_LIMIT_EXHAUSTED):
            # Neither is a statement about provider health. A 400 is about our
            # request or a permanently unavailable model; an exhausted free
            # pool is about our account. Tripping the breaker on either would
            # blame the provider for something it did not do.
            pass
        else:
            b.record_failure()

    def reset(self, provider: str, model: str) -> None:
        self.breaker(provider, model).reset()
        # An operator resetting a route means "try it again now", which has to
        # include un-parking it — otherwise the reset silently does nothing for
        # the one case an operator is most likely to be reacting to.
        self._parked.pop(self.key(provider, model), None)

    def snapshot(self) -> Dict[str, Any]:
        return {
            k: {"health": w.to_dict(),
                "circuit": self._breakers[k].to_dict() if k in self._breakers else None,
                "parked": self._parked_detail(k)}
            for k, w in self._windows.items()
        }

    def _parked_detail(self, key: str) -> Optional[Dict[str, Any]]:
        entry = self._parked.get(key)
        if entry is None:
            return None
        until, reason = entry
        remaining = until - time.time()
        if remaining <= 0:
            return None
        return {"reason": reason, "remaining_s": round(remaining, 1)}
