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

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..health import RetryPolicy, RouteHealth
from .. import single_model
from ..providers.registry import Registry, build_default_registry
from ..roles import ALIASES, PRIMARY_ALIAS, Role, role_for_alias, validate_preferences
from ..providers.local import local_only
from ..router import NoRouteAvailable, Route, Router, default_chain

# Requests naming a role alias select that role's chain; naming a concrete
# model pins that route; anything unrecognised falls to the default role.
BROKER_VERSION = "1.1.0"


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


def _retry_policy() -> RetryPolicy:
    """
    Retry limits sized for the mode this process is in.

    The defaults were measured against cloud routes averaging 90 s per request.
    A local generation is 154 s, which makes four attempts a poor bound and the
    240 s ceiling shorter than two attempts -- so it fires after the first retry
    whatever else happens.

    Locally there is also usually one model. "Move on to the next route" then
    means unloading 14 GB and loading another, so a retry is not buying a
    different outcome, only the same model another two and a half minutes.
    """
    if local_only():
        return RetryPolicy(max_attempts=2, max_route_seconds=900.0)
    return RetryPolicy(max_attempts=4)


class BrokerState:
    def __init__(self, registry: Optional[Registry] = None) -> None:
        self.registry = registry or Registry(build_default_registry())
        self.router = Router(
            self.registry,
            # Empty in local-only mode: see router.default_chain(). The chain
            # is then exactly what startup discovery found, so what /health
            # reports is what will actually be tried.
            chain=default_chain(),
            retry=_retry_policy(),
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

    @asynccontextmanager
    async def _lifespan(_: FastAPI):
        # One shared client: connection reuse matters when a single evolution
        # run makes thousands of calls.
        state.client = httpx.AsyncClient(
            timeout=httpx.Timeout(180.0, connect=15.0),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
        )
        # In local-only mode the chain starts empty of usable routes: a local
        # server's model ids are knowable ONLY by asking it, so a broker that
        # serves before discovering has nothing to route to and fails every
        # request. Discovery here is four requests to localhost, and a server
        # that is not running refuses the connection immediately.
        #
        # Bounded, because "refused instantly" is the good case: a process
        # holding the port open without answering would otherwise hold the
        # socket closed for as long as it liked.
        if local_only():
            try:
                await asyncio.wait_for(
                    state.registry.discover(state.client), timeout=20.0)
                state.router.refresh_chains()
            except Exception:
                # Same contract as the background probe: a failed startup
                # discovery must not stop the broker serving. /health reports
                # the empty chain honestly, and POST /v1/oe-max/verify re-runs
                # it once the operator starts their server.
                pass

        task: Optional[asyncio.Task] = None
        if verify_on_start:
            # Deliberately NOT awaited. Verification smoke-tests every model on
            # every usable provider, and each probe is a real completion: with
            # the shipped catalogue it took ~65 seconds before the socket
            # accepted anything, and it grows with every credential added.
            #
            # Meanwhile `run-evolution.sh` gives up on /health after 5 seconds
            # and tells the operator the broker is not running — which is both
            # wrong and the most confusing possible message, since they just
            # started it.
            #
            # Serving immediately with unverified beliefs is what the broker
            # does without `--verify` anyway, and those beliefs are corrected
            # the moment the probe finishes. `verified_at` stays null until
            # then, so nothing claims to be verified before it is.
            task = asyncio.create_task(_verify_in_background(state))
        try:
            yield
        finally:
            if task is not None and not task.done():
                task.cancel()
            if state.client is not None:
                await state.client.aclose()

    app = FastAPI(title="OE-MAX Broker", version=BROKER_VERSION,
                  lifespan=_lifespan)
    app.state.oe = state

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
        # Aliases first: they are what a client should normally name, and a
        # client picking the first entry of /v1/models gets a routed chain with
        # failover rather than a single pinned provider.
        data = [
            {"id": alias, "object": "model", "created": now, "owned_by": "oe-max",
             "oe_max": {"alias_for_role": role.value,
                        "chain": [f"{pr}/{mk}" for pr, mk
                                  in state.router.chains.get(role, [])]}}
            for alias, role in ALIASES.items()
        ]
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

        # Single-model mode overrides everything, including a per-request
        # pin. That is the point: "only this model answers" is not a preference
        # the caller gets to argue with, or the mode cannot be trusted to mean
        # what it says.
        single_route, single_reason = single_model.active_route(state.registry)
        if single_reason != "off" and single_route is None:
            # The mode is ON and cannot be satisfied. Fail loudly rather than
            # serving from a chain -- a run that reports one model while three
            # answered is the exact failure this mode exists to prevent.
            state.requests_failed += 1
            raise HTTPException(status_code=503, detail={
                "message": "single-model mode is on but its selection cannot "
                           "be served",
                "selected": single_model.selection(),
                "reason": single_reason,
            })

        pinned = single_route or _resolve_pinned(state.registry, req.model)
        role = None if pinned is not None else role_for_alias(req.model)
        try:
            if pinned is not None:
                # Pinned means "this route, no failover" — not "no policy".
                # Going straight to provider.chat would drop retry and
                # truncation escalation, so a pinned reasoning model would
                # truncate where the same model on the chain succeeds, and any
                # A/B between routes would measure the policy difference
                # instead of the models.
                result = await state.router.chat_pinned(
                    state.client, pinned, messages, **params
                )
            else:
                result = await state.router.chat(
                    state.client, messages, role=role,
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
        stamped = result.to_log()
        stamped["role"] = role.value if role is not None else "pinned"
        body["oe_max"] = stamped
        return body

    # ------------------------------------------------------- operations
    @app.get("/v1/oe-max/status")
    async def status() -> Dict[str, Any]:
        return {
            "router": state.router.snapshot(),
            "registry": state.registry.snapshot(),
            "stats_by_route": state.router.stats_by_route(),
            "single_model": single_model.describe(state.registry),
        }

    @app.get("/v1/oe-max/single-model")
    async def single_model_get() -> Dict[str, Any]:
        """Current selection, plus everything that could be selected.

        The candidate list is here rather than in a separate endpoint because a
        picker needs both in the same breath, and because a selection is only
        meaningful against the routes that exist right now -- discovery can
        withdraw one between a page load and a click.
        """
        state_ = single_model.describe(state.registry)
        state_["candidates"] = [
            c.to_dict() for c in single_model.candidates(state.registry)
        ]
        return state_

    @app.post("/v1/oe-max/single-model")
    async def single_model_set(body: Dict[str, Any]) -> Dict[str, Any]:
        """Turn the mode on for one model, or off with a null/empty `model`.

        Validates before storing, so a typo is refused at the point the operator
        made it rather than at the next request. `409` for a query that matches
        nothing or several things -- the message names which, because "no route"
        when you meant to be more specific wastes the next five minutes.
        """
        query = (body or {}).get("model")
        if query is None or not str(query).strip():
            single_model.clear()
            return single_model.describe(state.registry)

        route, reason = single_model.resolve(state.registry, str(query))
        if route is None:
            raise HTTPException(status_code=409, detail={
                "message": "cannot select that model",
                "requested": query,
                "reason": reason,
            })
        single_model.select(str(query))
        return single_model.describe(state.registry)

    @app.post("/v1/oe-max/verify")
    async def verify(check_tools: bool = True) -> Dict[str, Any]:
        """Re-run live discovery and smoke tests. Listing is not proof."""
        if state.client is None:
            raise HTTPException(status_code=503, detail="broker not started")
        discovered = await state.registry.discover(state.client)
        # Discovery can create routes that did not exist when the chains were
        # built — a catalogue provider has no models until its listing is
        # fetched. Rebuilding here is what makes a newly credentialled provider
        # actually reachable instead of merely present.
        chain_sizes = state.router.refresh_chains()
        probes = await state.registry.verify(state.client, check_tools=check_tools)
        state.verified_at = time.time()
        return {
            "discovered_counts": {k: len(v) for k, v in discovered.items()},
            "reconciled": state.registry.reconciled,
            "chain_sizes": chain_sizes,
            "probes": [p.to_dict() for p in probes],
            "eligible_routes": state.router.snapshot()["eligible"],
        }

    @app.post("/v1/oe-max/reset-circuit")
    async def reset_circuit(provider: str, model: str) -> Dict[str, Any]:
        state.router.health.reset(provider, model)
        return {"reset": f"{provider}/{model}"}

    return app


async def _verify_in_background(state: "BrokerState") -> None:
    """
    Discover and smoke-test without holding up the socket.

    Failures are swallowed for the same reason they were when this ran inline:
    a failed startup probe must not stop the broker serving. `/health` reports
    `verified_at: null`, which is the honest state — we have not verified,
    rather than we verified and found nothing.
    """
    try:
        await state.registry.discover(state.client)
        # Discovery can create routes that did not exist when the chains were
        # built: a catalogue provider has no models until its listing is
        # fetched, so without this a newly credentialled provider would be
        # discovered and never routed to.
        state.router.refresh_chains()
        await state.registry.verify(state.client)
        state.verified_at = time.time()
    except asyncio.CancelledError:
        raise
    except Exception:
        pass


def _resolve_pinned(registry: Registry, model: str) -> Optional[Route]:
    """
    If the caller named a concrete configured model, pin that route.

    Anything else — a role alias, or a name we do not recognise — goes through
    a chain, so a client that has not been told about the aliases still works.
    """
    if not model or model in ALIASES:
        return None
    for p in registry.providers.values():
        if not p.usable():
            continue
        for spec in p.models.values():
            if spec.id == model or spec.key == model:
                return Route(provider=p.name, model_key=spec.key, model_id=spec.id)
    return None


def _status_for(upstream_status: Optional[int]) -> int:
    if upstream_status in (429, 401, 403, 400):
        return upstream_status
    return 502
