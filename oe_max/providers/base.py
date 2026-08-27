"""
Provider adapter interface.

One generic OpenAI-compatible adapter serves OpenCode Zen, OpenRouter and
NVIDIA NIM, because all three speak the same wire protocol. Differences that do
matter — credentials, rate contract, health state, model identity — are
configuration on the adapter, not separate classes.

That is deliberate. Three near-identical subclasses would drift, and the spec is
explicit that provider/model identities belong in configuration rather than in
application logic, so that replacing a stealth-preview model never requires an
architectural change.

The adapter owns upstream credentials. Nothing below it — candidate code,
evaluators, sandboxes — ever sees a key.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

import httpx


class ProviderRole(str, Enum):
    PRIMARY = "primary"
    OX_FALLBACK = "ox_fallback"
    SPECIALIST_AND_FALLBACK = "specialist_and_fallback"
    LOCAL = "local"


class Outcome(str, Enum):
    OK = "ok"
    RATE_LIMITED = "rate_limited"       # 429 — slow down and retry
    FREE_LIMIT_EXHAUSTED = "free_limit_exhausted"   # 429, but the free pool is empty
    UNAVAILABLE = "unavailable"         # 503 / upstream down
    AUTH_FAILED = "auth_failed"         # 401 / 403
    BAD_REQUEST = "bad_request"         # 400 — often "model is unavailable"
    TIMEOUT = "timeout"
    TRANSPORT_ERROR = "transport_error"
    SERVER_ERROR = "server_error"
    TRUNCATED = "truncated"           # finish_reason=length — output cut off


# Outcomes worth retrying on another attempt or another provider. A 400 is not
# retryable on the same model — Zen returns it for "Model is unavailable", which
# no amount of retrying fixes.
RETRYABLE = frozenset({
    Outcome.RATE_LIMITED, Outcome.UNAVAILABLE, Outcome.TIMEOUT,
    Outcome.TRANSPORT_ERROR, Outcome.SERVER_ERROR, Outcome.TRUNCATED,
})

# A free allowance that is spent is NOT retryable, even though it arrives as a
# 429 like an ordinary rate limit. Waiting a second and asking again cannot
# refill a monthly pool, so retrying burns the whole retry budget to earn the
# identical error four times and then fails over anyway — several seconds later
# than it needed to. Measured live 2026-08-26: `mimo-v2.5-free` on OpenCode Zen
# answers every request with 429 `FreeUsageLimitError` regardless of spacing.
#
# The route is parked instead (`RouteHealth.park`), so the chain skips it
# entirely until the cooldown expires rather than rediscovering it each call.
FREE_LIMIT_MARKERS = (
    "freeusagelimit",       # OpenCode Zen
    "insufficient_quota",   # OpenAI-shaped providers
    "insufficient balance",
    "quota exceeded",
    "exceeded your current quota",
    "free tier limit",
    "out of credits",
)


def looks_like_free_limit(text: str) -> bool:
    """Whether a 429 body says the allowance is gone rather than too fast."""
    low = (text or "").lower()
    return any(marker in low for marker in FREE_LIMIT_MARKERS)


@dataclass
class ChatResult:
    """Normalised result of one attempt against one provider/model."""

    outcome: Outcome
    provider: str
    model: str
    latency_ms: float
    status_code: Optional[int] = None
    body: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    retry_after: Optional[float] = None
    attempt: int = 1

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.OK

    @property
    def usage(self) -> Dict[str, int]:
        return (self.body or {}).get("usage") or {}

    @property
    def reasoning_tokens(self) -> int:
        """Completion tokens spent on hidden reasoning, when reported."""
        details = (self.usage or {}).get("completion_tokens_details") or {}
        try:
            return int(details.get("reasoning_tokens") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def finish_reason(self) -> Optional[str]:
        try:
            return self.body["choices"][0].get("finish_reason")
        except (KeyError, IndexError, TypeError):
            return None

    @property
    def content(self) -> str:
        try:
            return (self.body["choices"][0]["message"].get("content") or "")
        except (KeyError, IndexError, TypeError):
            return ""

    def to_log(self) -> Dict[str, Any]:
        """Provenance record — every request records which provider served it."""
        return {
            "provider": self.provider, "model": self.model,
            "outcome": self.outcome.value, "status": self.status_code,
            "latency_ms": round(self.latency_ms, 1), "attempt": self.attempt,
            "usage": self.usage, "reasoning_tokens": self.reasoning_tokens,
            "finish_reason": self.finish_reason, "error": self.error,
        }


@dataclass
class ModelSpec:
    """A model as configured. IDs live here, never as application constants."""

    key: str                       # local handle, e.g. "ox_alpha"
    id: str                        # wire id, e.g. "x-preview-f-free"
    priority: int = 100            # higher wins
    ephemeral_preview: bool = False
    supports_tools: Optional[bool] = None    # None = not yet probed
    available: Optional[bool] = None         # None = not yet probed
    notes: str = ""
    observed_latency_ms: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


class ProviderAdapter:
    """An OpenAI-compatible upstream provider."""

    def __init__(
        self,
        name: str,
        base_url: str,
        *,
        role: ProviderRole = ProviderRole.PRIMARY,
        api_key_env: Optional[str] = None,
        limiter: Any = None,
        models: Optional[Dict[str, ModelSpec]] = None,
        timeout_s: float = 120.0,
        enabled: bool = True,
        requires_key: bool = True,
        public_listing: bool = False,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.role = role
        self.api_key_env = api_key_env
        self.models: Dict[str, ModelSpec] = models or {}
        self.enabled = enabled
        self.requires_key = requires_key
        # Whether `GET /models` answers without a credential. Verified true for
        # NVIDIA NIM on 2026-08-26 (83 models returned, no Authorization
        # header), which is worth exploiting: it lets us tell an operator that
        # a configured model no longer exists *before* they obtain a key, and
        # it is how the NIM ids in the registry are checkable at all.
        self.public_listing = public_listing
        # Generous default: Ox Alpha was observed taking 25s for a trivial
        # completion, so a typical 30s client timeout would fail healthy calls.
        self.timeout_s = timeout_s

        from ..limiter import NullLimiter

        self.limiter = limiter if limiter is not None else NullLimiter(name)

    # -- credentials ---------------------------------------------------

    @property
    def api_key(self) -> Optional[str]:
        return os.environ.get(self.api_key_env) if self.api_key_env else None

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)

    def usable(self) -> bool:
        """
        Whether this provider can be attempted at all.

        `requires_key=False` matters in practice: OpenCode Zen was observed
        serving `x-preview-f-free` with no Authorization header at all, so
        demanding a key would disable a working primary route.
        """
        if not self.enabled:
            return False
        return self.has_key or not self.requires_key

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        key = self.api_key
        if key:
            h["Authorization"] = f"Bearer {key}"
        return h

    # -- calls ---------------------------------------------------------

    async def list_models(self, client: httpx.AsyncClient) -> List[str]:
        """Live model discovery. Never trust a hardcoded list."""
        r = await client.get(f"{self.base_url}/models", headers=self._headers(),
                             timeout=30.0)
        r.raise_for_status()
        return [m.get("id") for m in (r.json().get("data") or []) if m.get("id")]

    async def chat(
        self,
        client: httpx.AsyncClient,
        model_id: str,
        messages: List[Dict[str, Any]],
        *,
        attempt: int = 1,
        **params: Any,
    ) -> ChatResult:
        """
        One attempt. Acquires a rate-limit slot first — including for retries,
        which is why `attempt` is passed in rather than looped here.
        """
        await self.limiter.acquire()

        body: Dict[str, Any] = {"model": model_id, "messages": messages}
        body.update({k: v for k, v in params.items() if v is not None})

        t0 = time.perf_counter()
        try:
            r = await client.post(
                f"{self.base_url}/chat/completions", json=body,
                headers=self._headers(), timeout=self.timeout_s,
            )
        except httpx.TimeoutException as e:
            return ChatResult(Outcome.TIMEOUT, self.name, model_id,
                              (time.perf_counter() - t0) * 1000,
                              error=str(e)[:200], attempt=attempt)
        except httpx.HTTPError as e:
            return ChatResult(Outcome.TRANSPORT_ERROR, self.name, model_id,
                              (time.perf_counter() - t0) * 1000,
                              error=f"{type(e).__name__}: {e}"[:200], attempt=attempt)

        latency = (time.perf_counter() - t0) * 1000
        retry_after = _parse_retry_after(r.headers.get("retry-after"))

        if r.status_code == 200:
            try:
                payload = r.json()
            except ValueError:
                return ChatResult(Outcome.SERVER_ERROR, self.name, model_id, latency,
                                  status_code=200, error="200 with non-JSON body",
                                  attempt=attempt)
            # A 200 can still carry a provider-level error object.
            if isinstance(payload, dict) and payload.get("error") and not payload.get("choices"):
                return ChatResult(Outcome.SERVER_ERROR, self.name, model_id, latency,
                                  status_code=200,
                                  error=str(payload["error"])[:300], attempt=attempt)
            self.limiter.recover()

            # Reasoning models spend part of the completion budget on hidden
            # reasoning, so a nominally successful 200 can still be cut off
            # mid-output. Observed with Ox Alpha: 961 of 1598 completion tokens
            # were reasoning, and the visible diff was truncated — a 130-second
            # request that produced nothing usable. Surfacing this as a
            # retryable outcome lets the router re-ask with a bigger budget
            # instead of silently wasting the call.
            finish = None
            try:
                finish = payload["choices"][0].get("finish_reason")
            except (KeyError, IndexError, TypeError):
                pass
            if finish == "length":
                return ChatResult(
                    Outcome.TRUNCATED, self.name, model_id, latency,
                    status_code=200, body=payload, attempt=attempt,
                    error="output truncated (finish_reason=length); "
                          "reasoning tokens may have consumed the budget",
                )

            return ChatResult(Outcome.OK, self.name, model_id, latency,
                              status_code=200, body=payload, attempt=attempt)

        outcome = _classify(r.status_code)
        if outcome is Outcome.RATE_LIMITED and looks_like_free_limit(r.text):
            # Distinguished here rather than in `_classify` because only the
            # body can tell the two 429s apart, and confusing them is expensive
            # in both directions: retrying an exhausted pool wastes the budget,
            # and parking a genuine rate limit would drop a working route.
            outcome = Outcome.FREE_LIMIT_EXHAUSTED
        if outcome in (Outcome.RATE_LIMITED, Outcome.UNAVAILABLE):
            # Deliberately not penalising the limiter for an exhausted free
            # pool: the limiter is shared across every model on the provider,
            # and one model's spent allowance says nothing about how fast the
            # others may be called.
            self.limiter.penalise(retry_after=retry_after)
        return ChatResult(outcome, self.name, model_id, latency,
                          status_code=r.status_code, error=r.text[:300].strip(),
                          retry_after=retry_after, attempt=attempt)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "base_url": self.base_url, "role": self.role.value,
            "enabled": self.enabled, "requires_key": self.requires_key,
            "api_key_env": self.api_key_env, "key_present": self.has_key,
            "usable": self.usable(), "public_listing": self.public_listing,
            "timeout_s": self.timeout_s,
            "models": {k: m.to_dict() for k, m in self.models.items()},
            "limiter": self.limiter.snapshot(),
        }


def _classify(status: int) -> Outcome:
    if status == 429:
        return Outcome.RATE_LIMITED
    if status in (401, 403):
        return Outcome.AUTH_FAILED
    if status == 400:
        return Outcome.BAD_REQUEST
    if status in (502, 503, 504):
        return Outcome.UNAVAILABLE
    if status >= 500:
        return Outcome.SERVER_ERROR
    return Outcome.BAD_REQUEST


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        # HTTP-date form; treat as a short backoff rather than failing to parse.
        return 5.0
