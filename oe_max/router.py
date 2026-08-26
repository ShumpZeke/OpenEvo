"""
Route selection and failover.

Routing is a chain, not a single choice: Ox Alpha first (the operator's stated
primary), then the alternate Ox route, then the strongest verified fallback.
The chain is data, so replacing a stealth-preview model is a config edit rather
than a code change — which the spec requires precisely because Ox Alpha may
vanish.

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


@dataclass
class Route:
    provider: str
    model_key: str
    model_id: str

    def __str__(self) -> str:
        return f"{self.provider}/{self.model_id}"


# Default chain. Ox Alpha through Zen leads; OpenRouter is the alternate Ox
# route; NIM is the specialist/fallback pool. Free Zen routes sit between so a
# NIM key is not required for the system to keep working.
DEFAULT_CHAIN: List[Tuple[str, str]] = [
    ("opencode_zen", "ox_alpha"),
    ("openrouter", "ox_alpha"),
    ("opencode_zen", "nemotron_ultra"),
    ("opencode_zen", "nemotron_lightning"),
    ("opencode_zen", "laguna"),
    ("opencode_zen", "hy3"),
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
    ) -> None:
        self.registry = registry
        self.chain = list(chain if chain is not None else DEFAULT_CHAIN)
        self.retry = retry or RetryPolicy()
        self.health = health or RouteHealth()
        self.request_log: List[Dict[str, Any]] = []
        self.max_log = 2000

    # -- selection -----------------------------------------------------

    def candidates(self, require_tools: bool = False) -> Tuple[List[Route], Dict[str, str]]:
        routes: List[Route] = []
        reasons: Dict[str, str] = {}
        degraded: List[Tuple[Route, str, str]] = []

        for provider_name, model_key in self.chain:
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
        **params: Any,
    ) -> ChatResult:
        """
        Try the chain until one route succeeds.

        Each attempt acquires its own rate-limit slot, including retries, so the
        NIM contract holds across the whole failover path rather than only the
        first try.
        """
        routes, reasons = self.candidates(require_tools=require_tools)
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

        for attempt in range(1, self.retry.max_attempts + 1):
            result = await provider.chat(
                client, route.model_id, messages, attempt=attempt, **attempt_params
            )
            self.health.record(result)
            self._log(result, route)

            if result.ok:
                return result

            if result.outcome not in RETRYABLE:
                break   # 400/401 will not improve by retrying this route

            if result.outcome is Outcome.TRUNCATED:
                # Retrying a truncation with the same budget reproduces it
                # exactly. Reasoning models spend part of the completion
                # budget invisibly — Ox Alpha was measured burning 961 of
                # 1598 completion tokens on reasoning — so the fix is a
                # bigger budget, not another identical call.
                grown = _grow_token_budget(attempt_params)
                if grown is None:
                    break   # already at the ceiling; give up on this route
                attempt_params = grown
                continue    # escalate immediately, no backoff needed

            if attempt < self.retry.max_attempts:
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
        return {
            "chain": [f"{p}/{m}" for p, m in self.chain],
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
