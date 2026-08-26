"""
Health- and capability-aware model router.

Selection is a filter followed by a sort:

  filter   the model must be enabled, hold its credential, satisfy the role's
           required capabilities, and not be in an open circuit
  sort      by (role-chain position, live health score, priority)

The capability filter is the part that ordinary "health-aware routing" misses.
Ox Alpha is healthy *and* the operator's preferred model *and* unable to serve a
tools request today (issue #44300). Health alone would keep routing agent work
to it and every agent run would fail. Capability filtering routes completion
work to it — where it is genuinely preferred — and agent work elsewhere.

Health is a rolling window per model: success rate, latency, and 429 pressure.
A model that starts failing sheds traffic gradually and is removed entirely once
its circuit opens, then probed again after a cooldown.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Sequence, Tuple

from ..telemetry.bus import emit
from ..telemetry.events import Component, Event, EventType, Status
from .profiles import (
    Capability,
    ModelProfile,
    Role,
    TOOL_REQUIRING_ROLES,
    default_profiles,
    default_role_chains,
)


@dataclass
class HealthWindow:
    """Rolling health for one model."""

    window: int = 50
    outcomes: Deque[bool] = field(default_factory=lambda: deque(maxlen=50))
    latencies: Deque[float] = field(default_factory=lambda: deque(maxlen=50))
    rate_limits: Deque[float] = field(default_factory=lambda: deque(maxlen=50))
    consecutive_failures: int = 0
    circuit_open_until: float = 0.0
    total_requests: int = 0
    total_failures: int = 0
    total_rate_limited: int = 0
    total_tokens: int = 0
    in_flight: int = 0
    last_used: float = 0.0
    last_error: Optional[str] = None

    def record(self, ok: bool, latency_ms: Optional[float] = None,
               rate_limited: bool = False, tokens: int = 0,
               error: Optional[str] = None) -> None:
        self.outcomes.append(ok)
        self.total_requests += 1
        self.last_used = time.time()
        if latency_ms is not None:
            self.latencies.append(latency_ms)
        if rate_limited:
            self.rate_limits.append(time.time())
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
        # An unused model is optimistic, not pessimistic: otherwise a fresh
        # fallback could never win a route and would never get to prove itself.
        if not self.outcomes:
            return 1.0
        return sum(self.outcomes) / len(self.outcomes)

    @property
    def p50_latency_ms(self) -> Optional[float]:
        if not self.latencies:
            return None
        s = sorted(self.latencies)
        return s[len(s) // 2]

    @property
    def recent_rate_limits(self) -> int:
        cutoff = time.time() - 60.0
        return sum(1 for t in self.rate_limits if t >= cutoff)

    def is_open(self) -> bool:
        return time.time() < self.circuit_open_until

    def score(self) -> float:
        """0..1, higher is better. Blends reliability, throttling and latency."""
        s = self.success_rate
        # 429s are a stronger negative than plain failures: they signal the
        # route will keep refusing, not that one call was unlucky.
        s -= min(0.5, 0.1 * self.recent_rate_limits)
        p50 = self.p50_latency_ms
        if p50 is not None:
            # Mild latency penalty; correctness dominates speed here.
            s -= min(0.2, (p50 / 60000.0))
        return max(0.0, min(1.0, s))

    def to_dict(self) -> Dict[str, object]:
        return {
            "success_rate": round(self.success_rate, 4),
            "score": round(self.score(), 4),
            "p50_latency_ms": self.p50_latency_ms,
            "recent_rate_limits": self.recent_rate_limits,
            "consecutive_failures": self.consecutive_failures,
            "circuit_open": self.is_open(),
            "circuit_open_until": self.circuit_open_until or None,
            "total_requests": self.total_requests,
            "total_failures": self.total_failures,
            "total_rate_limited": self.total_rate_limited,
            "total_tokens": self.total_tokens,
            "in_flight": self.in_flight,
            "last_used": self.last_used or None,
            "last_error": self.last_error,
        }


class NoRouteAvailable(RuntimeError):
    """Raised when no model can serve a role. Carries why each was excluded."""

    def __init__(self, role: Role, reasons: Dict[str, str]) -> None:
        self.role = role
        self.reasons = reasons
        detail = "; ".join(f"{k}: {v}" for k, v in reasons.items()) or "no profiles configured"
        super().__init__(f"no route for role '{role.value}' — {detail}")


class ModelRouter:
    def __init__(
        self,
        profiles: Optional[List[ModelProfile]] = None,
        role_chains: Optional[Dict[Role, List[str]]] = None,
        failure_threshold: int = 4,
        circuit_cooldown_s: float = 60.0,
        probe_ttl_s: float = 600.0,
    ) -> None:
        self.profiles: Dict[str, ModelProfile] = {
            p.id: p for p in (profiles if profiles is not None else default_profiles())
        }
        self.role_chains = role_chains if role_chains is not None else default_role_chains()
        self.health: Dict[str, HealthWindow] = {pid: HealthWindow() for pid in self.profiles}
        self.failure_threshold = failure_threshold
        self.circuit_cooldown_s = circuit_cooldown_s
        # How long a provider-doctor verdict counts as current. A failed probe
        # keeps a route out of selection for this long; after that the route is
        # unproven again rather than condemned, and gets to compete on live
        # traffic where the circuit breaker can judge it.
        self.probe_ttl_s = probe_ttl_s
        self._lock = threading.Lock()

    # -- selection -----------------------------------------------------

    def required_capabilities(self, role: Role) -> List[Capability]:
        caps = [Capability.CHAT]
        if role in TOOL_REQUIRING_ROLES:
            caps.append(Capability.TOOLS)
        return caps

    def candidates(self, role: Role) -> Tuple[List[ModelProfile], Dict[str, str]]:
        """Eligible profiles for a role, best first, plus exclusion reasons."""
        required = self.required_capabilities(role)
        chain = self.role_chains.get(role, [])
        excluded: Dict[str, str] = {}
        eligible: List[Tuple[Tuple[int, float, int], ModelProfile]] = []

        # Chain members first, then any other profile that lists the role.
        considered = list(dict.fromkeys(chain + [
            p.id for p in self.profiles.values() if role in p.roles
        ]))

        for pid in considered:
            prof = self.profiles.get(pid)
            if prof is None:
                excluded[pid] = "not configured"
                continue
            if not prof.enabled:
                excluded[pid] = "disabled"
                continue
            if not prof.usable():
                excluded[pid] = f"missing credential {prof.secret_ref}"
                continue
            if prof.probe_is_fresh(self.probe_ttl_s) and prof.last_probe_ok is False:
                # The doctor measured this route failing, recently. Selecting it
                # anyway means spending real requests to rediscover a fact we
                # already paid for. Expires with the TTL so a recovered route
                # returns on its own.
                age = time.time() - prof.last_probe_at
                excluded[pid] = (
                    f"provider doctor found this route failing {age:.0f}s ago: "
                    f"{prof.last_probe_detail or 'no detail recorded'}"
                )
                continue
            missing = [c.value for c in required if not prof.supports(c)]
            if missing:
                # Say whether this is measured or merely assumed. "Ox Alpha
                # lacks tools" means something very different depending on
                # whether a probe established it or a default declared it.
                verified = prof.verified_capabilities is not None
                excluded[pid] = (
                    f"lacks capability: {', '.join(missing)}"
                    + (" (verified by provider doctor)" if verified
                       else " (declared default; not yet probed)")
                )
                continue
            h = self.health[pid]
            if h.is_open():
                excluded[pid] = (
                    f"circuit open for {h.circuit_open_until - time.time():.0f}s "
                    f"after {h.consecutive_failures} failures"
                )
                continue
            if h.in_flight >= prof.max_concurrency:
                excluded[pid] = f"at concurrency limit ({prof.max_concurrency})"
                continue
            chain_pos = chain.index(pid) if pid in chain else len(chain) + prof.priority
            # Negative score so higher health sorts first under ascending sort.
            eligible.append(((chain_pos, -h.score(), prof.priority), prof))

        eligible.sort(key=lambda t: t[0])

        # Explain every remaining profile too, not just the ones in the chain.
        # The operator's preferred model is the one they most want an answer
        # about ("why isn't Ox Alpha doing my deep coding?"), and it is exactly
        # the model that silently drops out of a chain it cannot satisfy.
        for pid, prof in self.profiles.items():
            if pid in excluded or any(p.id == pid for p in (c for _, c in eligible)):
                continue
            if not prof.enabled:
                excluded[pid] = "disabled"
                continue
            missing = [c.value for c in required if not prof.supports(c)]
            if missing:
                verified = prof.verified_capabilities is not None
                excluded[pid] = (
                    f"lacks capability: {', '.join(missing)}"
                    + (" (verified by provider doctor)" if verified
                       else " (declared; not yet probed)")
                    + (f" — {prof.notes.splitlines()[0]}" if prof.notes else "")
                )
            else:
                excluded[pid] = "not assigned to this role"

        return [p for _, p in eligible], excluded

    def select(self, role: Role) -> ModelProfile:
        with self._lock:
            cands, reasons = self.candidates(role)
            if not cands:
                raise NoRouteAvailable(role, reasons)
            chosen = cands[0]
            self.health[chosen.id].in_flight += 1
            return chosen

    def release(
        self,
        profile_id: str,
        ok: bool,
        latency_ms: Optional[float] = None,
        rate_limited: bool = False,
        tokens: int = 0,
        error: Optional[str] = None,
    ) -> None:
        """Record the outcome of a request and update the circuit."""
        with self._lock:
            h = self.health.get(profile_id)
            if h is None:
                return
            h.in_flight = max(0, h.in_flight - 1)
            h.record(ok, latency_ms, rate_limited, tokens, error)
            if not ok and h.consecutive_failures >= self.failure_threshold:
                # Back off harder the longer a model keeps failing.
                cooldown = self.circuit_cooldown_s * min(
                    8, 2 ** (h.consecutive_failures - self.failure_threshold)
                )
                h.circuit_open_until = time.time() + cooldown
                prof = self.profiles[profile_id]
                emit(Event(
                    type=EventType.SYSTEM_WARNING, component=Component.PROVIDER,
                    status=Status.WARNING,
                    summary=(f"circuit opened for {profile_id} "
                             f"({h.consecutive_failures} consecutive failures), "
                             f"cooling down {cooldown:.0f}s"),
                    metrics={"cooldown_s": cooldown,
                             "consecutive_failures": float(h.consecutive_failures)},
                    metadata={"profile_id": profile_id, "provider": prof.provider,
                              "model": prof.model, "last_error": error},
                ))

    # -- introspection -------------------------------------------------

    def route_table(self) -> List[Dict[str, object]]:
        """Exactly what the Models page renders — no separate view model."""
        out = []
        for role in Role:
            cands, reasons = self.candidates(role)
            out.append({
                "role": role.value,
                "required_capabilities": [c.value for c in self.required_capabilities(role)],
                "chain": self.role_chains.get(role, []),
                "selected": cands[0].id if cands else None,
                "eligible": [c.id for c in cands],
                "excluded": reasons,
            })
        return out

    def snapshot(self) -> Dict[str, object]:
        return {
            "profiles": [p.to_dict() for p in self.profiles.values()],
            "health": {pid: h.to_dict() for pid, h in self.health.items()},
            "routes": self.route_table(),
        }

    def force(self, role: Role, profile_id: str) -> None:
        """Operator override: pin a role to one model (manual force, section 16.1)."""
        if profile_id not in self.profiles:
            raise KeyError(profile_id)
        self.role_chains[role] = [profile_id] + [
            p for p in self.role_chains.get(role, []) if p != profile_id
        ]

    def reset_circuit(self, profile_id: str) -> None:
        with self._lock:
            h = self.health.get(profile_id)
            if h:
                h.circuit_open_until = 0.0
                h.consecutive_failures = 0
