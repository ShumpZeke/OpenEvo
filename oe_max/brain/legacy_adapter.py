"""
LegacyBrainPort — temporary adapter that wraps the existing provider system.

This keeps the repository runnable while the new OpenCode path is being built.
It is the ONLY place that is allowed to import the old provider code.

Migration order:
  1. Core depends on BrainPort (this file not needed for that)
  2. Tests that need a real LLM use LegacyBrainPort until the OpenCode bridge is ready
  3. After OpenCode path is verified, LegacyBrainPort and the entire
     oe_max.providers / oe_max.router / oe_max.limiter / control_plane.providers
     legacy is deprecated and deleted.

Behavior:
  - Converts a BrainRequest into the old router.chat(messages, ...) call.
  - Converts the old ChatResult into a BrainResponse.
  - Maps PolicyMode -> legacy role string only for logging/provenance, not routing.
  - Does NOT introduce new hardcoded model IDs — it reuses the existing registry
    verbatim via build_default_registry(). New model catalog knowledge must NOT be
    added here.

WARNING: Do not import this file from oe_max.brain.* core types. Importing it
from the evolution engine re-introduces provider coupling. Import it only from
entrypoints that explicitly opt into legacy mode (e.g. integration tests,
standalone CLI, or a --legacy flag).
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import httpx

from .capabilities import BrainCapabilities, Capability
from .policies import POLICY_INSTRUCTIONS
from .port import BrainPort, BrainPortError
from .types import BrainRequest, BrainResponse, PolicyMode


def _brain_request_to_messages(req: BrainRequest) -> List[Dict[str, Any]]:
    """
    Build an OpenAI-compatible messages array from a BrainRequest.
    The policy instruction + objective + parent context become the prompt.
    """
    system_parts: List[str] = []
    policy_text = POLICY_INSTRUCTIONS.get(req.policy, "")
    if policy_text:
        system_parts.append(policy_text)
    # Objective is the primary goal for the mutation
    if req.objective:
        system_parts.append(f"OBJECTIVE:\n{req.objective}")
    # Constraints
    if req.constraints:
        system_parts.append(f"CONSTRAINTS:\n{req.constraints}")
    # Structured output instruction
    if req.required_output_schema:
        system_parts.append(
            "You must produce output matching this JSON schema:\n"
            f"{req.required_output_schema}"
        )

    system_msg = "\n\n".join(p for p in system_parts if p)

    user_parts: List[str] = []
    if req.mutation_strategy:
        user_parts.append(f"MUTATION STRATEGY: {req.mutation_strategy}")
    # Compact parent code (patch-first)
    if req.parent_code:
        user_parts.append(f"PARENT PROGRAM (apply a small patch to this):\n```\n{req.parent_code}\n```")
    if req.parent_metrics:
        user_parts.append(f"PARENT METRICS: {req.parent_metrics}")
    # Compact context packet (only what caller included — no full history dump)
    if req.context:
        # Keep it bounded — caller should have constructed a compact packet
        ctx_str = "\n".join(f"{k}: {v}" for k, v in req.context.items())
        if ctx_str:
            user_parts.append(f"CONTEXT:\n{ctx_str}")

    user_msg = "\n\n".join(user_parts) if user_parts else (req.objective or "Proceed.")

    messages: List[Dict[str, Any]] = []
    if system_msg:
        messages.append({"role": "system", "content": system_msg})
    messages.append({"role": "user", "content": user_msg})
    return messages


class LegacyBrainPort(BrainPort):
    """
    Wraps the old Registry + Router so the core can call BrainPort without
    knowing about providers.

    This adapter owns ALL provider knowledge — nothing else should.
    """

    def __init__(self, registry: Optional[Any] = None, router: Optional[Any] = None) -> None:
        # Lazy imports — keep this file the only place that touches providers
        from oe_max.providers.registry import Registry, build_default_registry
        from oe_max.router import Router

        self._registry = registry or Registry(build_default_registry())
        self._router = router or Router(self._registry)
        self._client: Optional[httpx.AsyncClient] = None
        self._caps: Optional[BrainCapabilities] = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=600.0)
        return self._client

    async def generate(self, request: BrainRequest) -> BrainResponse:
        try:
            messages = _brain_request_to_messages(request)
            # Build params from budget — all provider-neutral
            params: Dict[str, Any] = {}
            if request.budget.max_tokens is not None:
                params["max_tokens"] = request.budget.max_tokens
            # Note: temperature/top_p are intentionally not set here — the
            # prompt policy should drive variability, not a hardcoded param.
            # Callers can pass them via request.extra if needed:
            if "temperature" in request.extra:
                params["temperature"] = request.extra["temperature"]
            if "response_format" in request.extra:
                params["response_format"] = request.extra["response_format"]
            if request.required_output_schema and "response_format" not in params:
                params["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "brain_output",
                        "schema": request.required_output_schema,
                    },
                }

            client = self._ensure_client()
            # Route via the old router — capability-aware where the router supports it
            require_tools = bool(request.extra.get("require_tools"))
            result = await self._router.chat(client, messages, require_tools=require_tools, **params)

            if not result.ok:
                retryable = result.outcome.value in ("rate_limited", "unavailable", "timeout", "transport_error", "server_error", "truncated")
                return BrainResponse.failure(
                    error=f"{result.outcome.value}: {result.error or ''} (provider={result.provider} model={result.model})",
                    latency_ms=result.latency_ms,
                )

            content = result.content or ""
            # Truncation is retryable with a larger budget
            truncated = result.outcome.value == "truncated" or result.finish_reason == "length"
            return BrainResponse(
                content=content,
                usage=dict(result.usage or {}),
                latency_ms=result.latency_ms,
                model_meta={
                    "provider": result.provider,
                    "model": result.model,
                    "status": result.status_code,
                    "finish_reason": result.finish_reason,
                },
                reasoning_tokens=result.reasoning_tokens if hasattr(result, "reasoning_tokens") else None,
                truncated=truncated,
                ok=not truncated,
                error="output truncated (finish_reason=length)" if truncated else None,
            )
        except Exception as exc:
            raise BrainPortError(f"legacy brain failed: {exc}", retryable=True) from exc

    async def capabilities(self) -> BrainCapabilities:
        if self._caps is not None:
            return self._caps
        # Legacy: report based on registry probes if available, else minimal + streaming
        caps = BrainCapabilities(
            text=True,
            tool_use=False,
            structured_output=False,
            streaming=False,
            cancellation=False,
            context_limit=128000,  # conservative assumption for legacy
            output_limit=4096,
            host_model_id=None,
            host_provider_id="legacy",
            extra={"mode": "legacy"},
        )
        self._caps = caps
        return caps

    async def health_check(self) -> bool:
        # Consider healthy if at least one provider has a usable route
        routes, _ = self._router.candidates()
        return len(routes) > 0

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None
