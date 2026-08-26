"""
OE-MAX broker — a local OpenAI-compatible endpoint.

OpenEvolve is configured with `api_base: http://127.0.0.1:8787/v1` and a single
model alias. Everything about provider identity, routing, rate limiting,
failover and retry lives here, behind that stable interface.

Why this shape rather than teaching OpenEvolve about providers:

  * Upstream stays untouched. It already speaks the OpenAI protocol, so pointing
    it at a different base URL needs zero code changes — the patch surface stays
    empty and upstream merges keep fast-forwarding.
  * Credentials live in exactly one process. Candidate code and evaluators run
    with no keys in their environment at all, which is what makes the
    anti-reward-hacking boundary meaningful.
  * The rate contract is enforced at the single point every request passes
    through. A limiter inside N worker processes is N limiters.
  * Swapping a stealth-preview model becomes a config edit.

Routes: GET /health · GET /v1/models · POST /v1/chat/completions
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..health import RetryPolicy, RouteHealth
from ..providers.registry import Registry, build_default_registry
from ..router import DEFAULT_CHAIN, NoRouteAvailable, Router

# The alias OpenEvolve is configured with. Requests naming it (or anything
# unrecognised) go through the chain; naming a concrete model pins that route.
PRIMARY_ALIAS = "oe-max-primary"
BROKER_VERSION = "1.0.0"


class ChatMessage(BaseModel):
    role: str
    content: Any = None
    name: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    model: str = PRIMARY_ALIAS
    messages: List[ChatMessage]
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    max_completion_tokens: Optional[int] = None
    stop: Optional[Any] = None
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Any] = None
    response_format: Optional[Dict[str, Any]] = None
    stream: bool = False
    seed: Optional[int] = None
    n: Optional[int] = None
    # OpenEvolve and other clients send extra keys; accept and ignore rather
    # than 422-ing a request we could have served.
    model_config = {"extra": "allow"}


class BrokerState:
    def __init__(self, registry: Optional[Registry] = None) -> None:
        self.registry = registry or Registry(build_default_registry())
        self.router = Router(
            self.registry,
            chain=list(DEFAULT_CHAIN),
            retry=RetryPolicy(max_attempts=4),
            health=RouteHealth(),
        )
        self.started_at = time.time()
        self.requests_served = 0
        self.requests_failed = 0
        self.client: Optional[httpx.AsyncClient] = None
        self.verified_at: Optional[float] = None
        # Local token, if set, gates access to the broker itself. It is NOT an
        # upstream credential — upstream keys never leave this process.
        self.local_token = os.environ.get("OE_MAX_LOCAL_API_KEY")


def create_app(registry: Optional[Registry] = None,
               verify_on_start: bool = False) -> FastAPI:
    state = BrokerState(registry)
    app = FastAPI(title="OE-MAX Broker", version=BROKER_VERSION)
    app.state.oe = state

    @app.on_event("startup")
    async def _startup() -> None:
        # One shared client: connection reuse matters when a single evolution
        # run makes thousands of calls.
        state.client = httpx.AsyncClient(
            timeout=httpx.Timeout(180.0, connect=15.0),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
        )
        if verify_on_start:
            try:
                await state.registry.discover(state.client)
                await state.registry.verify(state.client)
                state.verified_at = time.time()
            except Exception:
                # A failed startup probe must not prevent the broker booting;
                # /health reports the unverified state.
                pass

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        if state.client is not None:
            await state.client.aclose()

    def _check_local_auth(request: Request) -> None:
        """
        Gate the broker with a local token when one is configured.

        This protects the broker from other processes on the machine; it is
        unrelated to upstream provider credentials.
        """
        if not state.local_token:
            return
        auth = request.headers.get("authorization", "")
        supplied = auth[7:] if auth.lower().startswith("bearer ") else None
        if supplied != state.local_token:
            raise HTTPException(status_code=401, detail="invalid local broker token")

    # ------------------------------------------------------------ health
    @app.get("/health")
    async def health() -> Dict[str, Any]:
        return {
            "status": "ok",
            "version": BROKER_VERSION,
            "uptime_s": round(time.time() - state.started_at, 1),
            "requests_served": state.requests_served,
            "requests_failed": state.requests_failed,
            "verified_at": state.verified_at,
            "primary_alias": PRIMARY_ALIAS,
            "providers": {
                name: {
                    "usable": p.usable(),
                    "role": p.role.value,
                    "key_present": p.has_key,
                    "requires_key": p.requires_key,
                    "limiter": p.limiter.snapshot(),
                }
                for name, p in state.registry.providers.items()
            },
            "routes": state.router.snapshot()["eligible"],
        }

    @app.get("/v1/models")
    async def list_models(request: Request) -> Dict[str, Any]:
        """
        Advertise the alias plus every configured model.

        The alias is listed first because that is what OpenEvolve is pointed at;
        concrete ids are listed so an operator can pin one deliberately.
        """
        _check_local_auth(request)
        now = int(time.time())
        data = [{"id": PRIMARY_ALIAS, "object": "model", "created": now,
                 "owned_by": "oe-max"}]
        for pname, p in state.registry.providers.items():
            for spec in p.models.values():
                data.append({
                    "id": spec.id, "object": "model", "created": now,
                    "owned_by": pname,
                    "oe_max": {
                        "model_key": spec.key, "priority": spec.priority,
                        "available": spec.available,
                        "supports_tools": spec.supports_tools,
                        "ephemeral_preview": spec.ephemeral_preview,
                    },
                })
        return {"object": "list", "data": data}

    # -------------------------------------------------- chat completions
    @app.post("/v1/chat/completions")
    async def chat_completions(req: ChatCompletionRequest, request: Request) -> Any:
        _check_local_auth(request)
        if state.client is None:
            raise HTTPException(status_code=503, detail="broker not started")
        if req.stream:
            # Honest refusal rather than a broken stream: OpenEvolve does not
            # require streaming, and pretending to support it would fail subtly.
            raise HTTPException(
                status_code=400,
                detail="streaming is not implemented by the OE-MAX broker; "
                       "set stream=false",
            )

        messages = [
            {k: v for k, v in m.model_dump().items() if v is not None}
            for m in req.messages
        ]
        params: Dict[str, Any] = {
            "temperature": req.temperature,
            "top_p": req.top_p,
            "max_tokens": req.max_tokens or req.max_completion_tokens,
            "stop": req.stop,
            "tools": req.tools,
            "tool_choice": req.tool_choice,
            "response_format": req.response_format,
            "seed": req.seed,
        }
        params = {k: v for k, v in params.items() if v is not None}

        pinned = _resolve_pinned(state.registry, req.model)
        try:
            if pinned is not None:
                provider, model_id = pinned
                result = await provider.chat(
                    state.client, model_id, messages, **params
                )
                state.router.health.record(result)
            else:
                result = await state.router.chat(
                    state.client, messages,
                    require_tools=bool(req.tools), **params
                )
        except NoRouteAvailable as e:
            state.requests_failed += 1
            raise HTTPException(status_code=503, detail={
                "message": "no usable provider route",
                "excluded": e.reasons,
            })

        if not result.ok:
            state.requests_failed += 1
            return JSONResponse(
                status_code=_status_for(result.status_code),
                content={"error": {
                    "type": result.outcome.value,
                    "message": result.error or "upstream request failed",
                    "oe_max": result.to_log(),
                }},
            )

        state.requests_served += 1
        body = dict(result.body or {})
        # Stamp provenance onto every response: the spec requires recording
        # which provider actually served each request.
        body["oe_max"] = result.to_log()
        return body

    # ------------------------------------------------------- operations
    @app.get("/v1/oe-max/status")
    async def status() -> Dict[str, Any]:
        return {
            "router": state.router.snapshot(),
            "registry": state.registry.snapshot(),
            "stats_by_route": state.router.stats_by_route(),
        }

    @app.post("/v1/oe-max/verify")
    async def verify(check_tools: bool = True) -> Dict[str, Any]:
        """Re-run live discovery and smoke tests. Listing is not proof."""
        if state.client is None:
            raise HTTPException(status_code=503, detail="broker not started")
        discovered = await state.registry.discover(state.client)
        probes = await state.registry.verify(state.client, check_tools=check_tools)
        state.verified_at = time.time()
        return {
            "discovered_counts": {k: len(v) for k, v in discovered.items()},
            "probes": [p.to_dict() for p in probes],
            "eligible_routes": state.router.snapshot()["eligible"],
        }

    @app.post("/v1/oe-max/reset-circuit")
    async def reset_circuit(provider: str, model: str) -> Dict[str, Any]:
        state.router.health.reset(provider, model)
        return {"reset": f"{provider}/{model}"}

    return app


def _resolve_pinned(registry: Registry, model: str):
    """
    If the caller named a concrete configured model, pin that route.

    Anything else — including the alias and unknown names — goes through the
    chain, so a client that has not been told about the alias still works.
    """
    if not model or model == PRIMARY_ALIAS:
        return None
    for p in registry.providers.values():
        if not p.usable():
            continue
        for spec in p.models.values():
            if spec.id == model or spec.key == model:
                return p, spec.id
    return None


def _status_for(upstream_status: Optional[int]) -> int:
    if upstream_status in (429, 401, 403, 400):
        return upstream_status
    return 502
