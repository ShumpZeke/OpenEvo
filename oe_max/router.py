"""
DEPRECATED — Legacy router, now behind oe_max.brain.legacy_adapter.

New code must use BrainPort. See oe_max/brain/README.md.

Route selection and failover.

Routing is a chain, not a single choice, and there is one chain per *role*
rather than one for everything — see `oe_max/roles.py` for why the free routes
differ in kind and not merely in quality.

The chain is data, so replacing a stealth-preview model is a config edit rather
than a code change. That provision was not theoretical: Ox Alpha, the
operator's stated primary and the head of every chain here, was withdrawn from
OpenCode Zen and probed gone on 2026-08-26. Replacing it was a table edit.

Selection filters, then orders:

  filter  provider usable · model believed available · circuit closed ·
          required capability satisfied
  order   explicit chain position, then live health, then configured priority

`allow_empirical_reallocation` is respected by exposing measured stats; the
weights are initialisation, not doctrine.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .health import RetryPolicy, RouteHealth
from .providers.base import ChatResult, Outcome, RETRYABLE, ProviderAdapter
from .providers.registry import Registry
from .roles import Role, build_chains


@dataclass
class Route:
    provider: str
    model_key: str
    model_id: str

    def __str__(self) -> str:
        return f"{self.provider}/{self.model_id}"


# Default chain — the role-agnostic order, used when no role is named and as
# the shared tail of every role chain.
#
# NIM first, by operator decision (2026-08-27) and by measurement: it is the
# only provider here whose models were probed individually with a real key, and
# the four ids that were in NVIDIA's catalogue but did not serve are already
# absent — see the registry for what each one did.
#
# The keyless Zen routes follow as the tail, not as a preference. `usable()`
# drops the NIM entries when NVIDIA_API_KEY is absent, so this single list
# serves both installs: with a key the run is on NIM, without one it still
# works rather than failing at the first request.
#
# Ox Alpha is deliberately absent. It was withdrawn by the provider on
# 2026-08-26 and has since been removed from service here entirely, including
# the alternate `stealth/ox-alpha` route that used to sit at the end of this
# list through OpenRouter.
DEFAULT_CHAIN: List[Tuple[str, str]] = [
    # NIM, ordered by measured latency 2026-08-28.
    ("nvidia_nim", "nemotron_super_120b"),
    ("nvidia_nim", "nemotron_ultra_253b"),
    ("nvidia_nim", "nemotron_nano_30b"),
    ("nvidia_nim", "kimi_k3"),
    ("nvidia_nim", "deepseek_v4_flash"),
    # Keyless fallback, so a checkout with no credential still runs.
    ("opencode_zen", "nemotron_ultra"),
    ("opencode_zen", "hy3"),
    ("opencode_zen", "laguna"),
    ("opencode_zen", "nemotron_lightning"),
]


class NoRouteAvailable(RuntimeError):
    def __init__(self, reasons: Dict[str, str]) -> None:
        self.reasons = reasons
        detail = "; ".join(f"{k}: {v}" for k, v in reasons.items()) or "no routes configured"
        super().__init__(f"no usable route — {detail}")


class Router:
    def __init__(
        self,
        registry: Registry,
        *,
        chain: Optional[List[Tuple[str, str]]] = None,
        retry: Optional[RetryPolicy] = None,
        health: Optional[RouteHealth] = None,
        chains: Optional[Dict[Role, List[Tuple[str, str]]]] = None,
    ) -> None:
        self.registry = registry
        self.chain = list(chain if chain is not None else DEFAULT_CHAIN)
        # Role chains are derived from the role-agnostic chain by default, so a
        # caller that customises `chain` gets consistent role behaviour for
        # free instead of silently keeping the shipped preferences.
        self.chains: Dict[Role, List[Tuple[str, str]]] = (
            chains if chains is not None else build_chains(self.chain)
        )
        self.retry = retry or RetryPolicy()
        self.health = health or RouteHealth()
        self.request_log: List[Dict[str, Any]] = []
        self.max_log = 2000

    def registry_routes(self) -> List[Tuple[str, str]]:
        """
        Every (provider, model_key) the registry currently knows, in a stable
        order: providers in registration order, models by descending priority.
        """
        out: List[Tuple[str, str]] = []
        for name, provider in self.registry.providers.items():
            for key, spec in sorted(
                provider.models.items(), key=lambda kv: -kv[1].priority
            ):
                out.append((name, key))
        return out

    def refresh_chains(self) -> Dict[str, int]:
        """
        Rebuild the role chains to include routes discovered since startup.

        Catalogue providers have no models until a listing is fetched, so a
        chain built at construction cannot contain them. Without this, adding
        GROQ_API_KEY would produce a provider that discovers models, reports
        them healthy, and is never routed to — the most confusing possible
        outcome, because everything looks configured and nothing uses it.

        The configured chain still leads. Discovered routes join the tail, so
        a new provider is a fallback until someone deliberately promotes it,
        rather than silently displacing a route that has been measured.
        """
        known = set(self.chain)
        tail = [r for r in self.registry_routes() if r not in known]
        self.chain = list(self.chain) + tail
        self.chains = build_chains(self.chain)
        return {role.value: len(chain) for role, chain in self.chains.items()}

    # -- selection -----------------------------------------------------

    def candidates(
        self, require_tools: bool = False, role: Optional[Role] = None,
    ) -> Tuple[List[Route], Dict[str, str]]:
        routes: List[Route] = []
        reasons: Dict[str, str] = {}
        degraded: List[Tuple[Route, str, str]] = []

        chain = self.chains.get(role, self.chain) if role is not None else self.chain
        for provider_name, model_key in chain:
            p = self.registry.provider(provider_name)
            label = f"{provider_name}/{model_key}"
            if p is None:
                reasons[label] = "provider not configured"
                continue
            if not p.usable():
                reasons[label] = (
                    f"missing credential {p.api_key_env}" if p.requires_key and not p.has_key
                    else "provider disabled"
                )
                continue
            spec = p.models.get(model_key)
            if spec is None:
                reasons[label] = "model not configured for this provider"
                continue
            if spec.available is False:
                reasons[label] = f"probe says unavailable: {spec.notes or 'no detail'}"
                continue
            if require_tools and spec.supports_tools is False:
                reasons[label] = "probed as not supporting tool calls"
                continue
            if not self.health.allow(p.name, spec.id):
                # Parking and the breaker both close a route, and reporting the
                # wrong one sends an operator to debug the wrong thing: a
                # parked route's breaker reads "closed, 0s remaining", which
                # says the route is fine while it is being skipped.
                parked = self.health.parked(p.name, spec.id)
                if parked:
                    reasons[label] = parked
                else:
                    br = self.health.breaker(p.name, spec.id).to_dict()
                    reasons[label] = (
                        f"circuit {br['state']}, {br['cooldown_remaining_s']}s remaining"
                    )
                continue
            route = Route(p.name, model_key, spec.id)
            why = self.health.degraded(p.name, spec.id)
            if why:
                # Held back rather than dropped: if every route is degraded,
                # the least-bad one still has to serve. A run that dies with
                # "no usable route" is worse than one served slowly by a
                # flaky provider.
                degraded.append((route, label, why))
                continue
            routes.append(route)

        if not routes and degraded:
            degraded.sort(key=lambda item: self.health.success_rate(
                item[0].provider, item[0].model_id), reverse=True)
            best, label, why = degraded[0]
            routes.append(best)
            reasons[label] = f"{why} — serving anyway: every route is degraded"
            for _, other_label, other_why in degraded[1:]:
                reasons[other_label] = other_why
        else:
            for _, label, why in degraded:
                reasons[label] = why

        return routes, reasons

    # -- execution -----------------------------------------------------

    async def chat(
        self,
        client: httpx.AsyncClient,
        messages: List[Dict[str, Any]],
        *,
        require_tools: bool = False,
        role: Optional[Role] = None,
        **params: Any,
    ) -> ChatResult:
        """
        Try the chain until one route succeeds.

        Each attempt acquires its own rate-limit slot, including retries, so the
        NIM contract holds across the whole failover path rather than only the
        first try.
        """
        routes, reasons = self.candidates(require_tools=require_tools, role=role)
        if not routes:
            raise NoRouteAvailable(reasons)

        last: Optional[ChatResult] = None
        for route in routes:
            result = await self._try_route(client, route, messages, params)
            if result.ok:
                return result
            last = result
            # exhausted this route; fall through to the next in the chain

        return last or ChatResult(
            Outcome.SERVER_ERROR, "router", "none", 0.0,
            error="all routes exhausted with no result",
        )

    async def chat_pinned(
        self,
        client: httpx.AsyncClient,
        route: Route,
        messages: List[Dict[str, Any]],
        **params: Any,
    ) -> ChatResult:
        """
        Send to one named route, with the same retry and truncation policy the
        chain applies — but no failover.

        Pinning exists so a caller can measure or require a specific model. If
        it also silently dropped budget escalation, a pinned reasoning model
        would truncate where the same model on the chain succeeds, and an A/B
        experiment between routes would be measuring the policy rather than the
        models. Measured: a 16-token budget made nemotron-3-ultra-free return
        `finish_reason=length` after spending 17 tokens on hidden reasoning —
        on the chain that same call escalates and returns an answer.
        """
        return await self._try_route(client, route, messages, params)

    async def _try_route(
        self,
        client: httpx.AsyncClient,
        route: Route,
        messages: List[Dict[str, Any]],
        params: Dict[str, Any],
    ) -> ChatResult:
        """
        One route, up to `retry.max_attempts` tries.

        Each attempt acquires its own rate-limit slot, including retries, so the
        NIM contract holds across the whole path rather than only the first try.
        """
        provider = self.registry.provider(route.provider)
        if provider is None:
            return ChatResult(
                Outcome.SERVER_ERROR, route.provider, route.model_id, 0.0,
                error=f"provider {route.provider!r} is not registered",
            )
        attempt_params = dict(params)
        result: Optional[ChatResult] = None
        started = time.monotonic()

        for attempt in range(1, self.retry.max_attempts + 1):
            result = await provider.chat(
                client, route.model_id, messages, attempt=attempt, **attempt_params
            )
            self.health.record(result)

            if result.ok:
                self._log(result, route)
                return result

            # Decided before logging, because the log entry is a snapshot: a
            # reason attached afterwards never reaches it, and the reason is
            # the whole value here. "Gave up after 4 attempts" and "gave up
            # after 4 minutes" point at different fixes.
            retryable = result.outcome in RETRYABLE
            give_up: Optional[str] = None
            grown: Optional[Dict[str, Any]] = None

            if retryable:
                if result.outcome is Outcome.TRUNCATED:
                    # Retrying a truncation with the same budget reproduces it
                    # exactly. Reasoning models spend part of the completion
                    # budget invisibly — Ox Alpha was measured burning 961 of
                    # 1598 completion tokens on reasoning — so the fix is a
                    # bigger budget, not another identical call.
                    grown = _grow_token_budget(attempt_params)
                    if grown is None:
                        give_up = "token budget already at the ceiling"
                if give_up is None:
                    # A doubled budget on a slow route is the most expensive
                    # retry there is, so escalation is bounded by the same
                    # wall-clock budget as everything else.
                    give_up = self.retry.exhausted(
                        attempt, time.monotonic() - started)
            else:
                # 400/401 and an exhausted free allowance. Deliberately NOT
                # annotated: these errors already say why they will not
                # improve, and "Model is unavailable [not retryable]" is noise
                # on a message that was already clear.
                give_up = ""

            if give_up:
                result.error = f"{result.error or ''} [{give_up}]".strip()

            self._log(result, route)

            if give_up or not retryable:
                break

            if result.outcome is Outcome.TRUNCATED:
                attempt_params = grown or attempt_params
                continue    # escalate immediately, no backoff needed

            await asyncio.sleep(
                self.retry.delay_for(attempt, result.retry_after)
            )

        return result or ChatResult(
            Outcome.SERVER_ERROR, route.provider, route.model_id, 0.0,
            error="route produced no result",
        )

    def _log(self, result: ChatResult, route: Route) -> None:
        entry = result.to_log()
        entry.update({"ts": time.time(), "model_key": route.model_key})
        self.request_log.append(entry)
        if len(self.request_log) > self.max_log:
            del self.request_log[: len(self.request_log) - self.max_log]

    # -- introspection -------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        routes, reasons = self.candidates()
        tool_routes, tool_reasons = self.candidates(require_tools=True)
        # Per-role: which route each role would actually reach right now.
        # The chain is the intent; this is the outcome, and they differ exactly
        # when something is unhealthy — which is when an operator looks.
        by_role: Dict[str, Any] = {}
        for role in Role:
            role_routes, role_reasons = self.candidates(role=role)
            by_role[role.value] = {
                "chain": [f"{p}/{m}" for p, m in self.chains.get(role, [])],
                "serving": str(role_routes[0]) if role_routes else None,
                "eligible": [str(r) for r in role_routes],
                "excluded": role_reasons,
            }

        return {
            "chain": [f"{p}/{m}" for p, m in self.chain],
            "roles": by_role,
            "eligible": [str(r) for r in routes],
            "eligible_with_tools": [str(r) for r in tool_routes],
            "excluded": reasons,
            "excluded_with_tools": tool_reasons,
            "health": self.health.snapshot(),
            "recent_requests": self.request_log[-50:],
        }

    def stats_by_route(self) -> Dict[str, Dict[str, Any]]:
        agg: Dict[str, Dict[str, Any]] = {}
        for e in self.request_log:
            k = f"{e['provider']}/{e['model']}"
            a = agg.setdefault(k, {"requests": 0, "ok": 0, "tokens": 0,
                                   "latency_sum": 0.0, "errors": {}})
            a["requests"] += 1
            if e["outcome"] == "ok":
                a["ok"] += 1
            else:
                a["errors"][e["outcome"]] = a["errors"].get(e["outcome"], 0) + 1
            a["tokens"] += int((e.get("usage") or {}).get("total_tokens") or 0)
            a["latency_sum"] += e.get("latency_ms") or 0.0
        for a in agg.values():
            a["success_rate"] = round(a["ok"] / a["requests"], 4) if a["requests"] else 0.0
            a["avg_latency_ms"] = (
                round(a["latency_sum"] / a["requests"], 1) if a["requests"] else None
            )
            del a["latency_sum"]
        return agg


# Token-budget escalation for truncated reasoning-model output.
TOKEN_GROWTH_FACTOR = 2.0
TOKEN_BUDGET_CEILING = 32000
DEFAULT_TOKEN_BUDGET = 4096


def _grow_token_budget(params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Double the completion budget for a retry after truncation.

    Returns None once the ceiling is reached, so the router stops re-asking a
    model that cannot fit its answer and moves to the next route instead of
    burning the rate budget on ever-larger failures.
    """
    current = params.get("max_tokens") or DEFAULT_TOKEN_BUDGET
    try:
        current = int(current)
    except (TypeError, ValueError):
        current = DEFAULT_TOKEN_BUDGET
    if current >= TOKEN_BUDGET_CEILING:
        return None
    grown = dict(params)
    grown["max_tokens"] = min(int(current * TOKEN_GROWTH_FACTOR), TOKEN_BUDGET_CEILING)
    return grown
