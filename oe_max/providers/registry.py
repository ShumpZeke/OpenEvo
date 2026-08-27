"""
DEPRECATED — Legacy provider registry, now behind oe_max.brain.legacy_adapter.

Do not add new models here. New code must use BrainPort (oe_max.brain).
This file will be removed after the OpenCode path is verified per migration order.
See oe_max/brain/README.md for the new architecture.

Provider and model registry, with live discovery.

The spec is emphatic that model identities are configuration, discovered and
smoke-tested at startup rather than baked in. Live probing on 2026-08-26 showed
exactly why:

  * `deepseek-v4-flash-free` appears in Zen's `/models` listing but returns
    HTTP 400 "Model is unavailable" — **being listed does not mean being
    serveable**, so discovery alone is not enough; each candidate must be
    smoke-tested.
  * `mimo-v2.5-free` returns 429 `FreeUsageLimitError` — free tiers carry real
    limits that differ per model.
  * `x-preview-f-free` (Ox Alpha) answered with **no Authorization header at
    all**, so requiring a key would have disabled the primary route.
  * Ox Alpha latency ranged 2s–25s with an intermittent 503, so health must be
    judged over several probes rather than one.

Discovery therefore has two stages: list what exists, then prove what works.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from ..limiter import NullLimiter, RateLimiter
from .base import ChatResult, ModelSpec, Outcome, ProviderAdapter, ProviderRole

# Verified live 2026-08-26.
OPENCODE_ZEN_BASE = "https://opencode.ai/zen/v1"
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
NVIDIA_NIM_BASE = "https://integrate.api.nvidia.com/v1"


@dataclass
class ProbeResult:
    provider: str
    model: str
    reachable: bool
    supports_tools: Optional[bool]
    latency_ms: Optional[float]
    status: Optional[int]
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


def build_default_registry(
    *,
    nim_hard_cap: int = 44,
    nim_target_rpm: float = 42.0,
    nim_burst: float = 2.0,
    nim_state_path: Optional[str] = None,
) -> Dict[str, ProviderAdapter]:
    """
    The shipped provider set.

    Only NIM gets a rate limiter: the spec says the 48 RPM contract belongs to
    the NIM account and must not be applied to Ox Alpha unless that provider
    independently requires it. Each provider owns its own scheduler and health.
    """
    zen = ProviderAdapter(
        name="opencode_zen",
        base_url=OPENCODE_ZEN_BASE,
        role=ProviderRole.PRIMARY,
        api_key_env="OPENCODE_ZEN_API_KEY",
        limiter=NullLimiter("opencode_zen"),
        # Observed serving without an Authorization header; a key is used when
        # present but its absence must not disable the primary route.
        requires_key=False,
        # Measured, not guessed. A trivial completion took up to 25s, but a real
        # mutation with a 16,000-token budget runs well past 180s — observed as
        # 6 timeouts in 12 requests with a 180s limit. For a reasoning model the
        # provider timeout has to scale with the token budget, so this is
        # deliberately generous; the router's retry path handles genuine hangs.
        timeout_s=600.0,
        models={
            "ox_alpha": ModelSpec(
                key="ox_alpha", id="x-preview-f-free", priority=100,
                ephemeral_preview=True,
                notes="Ox Alpha Free. Stealth preview, free for a limited time.",
            ),
            "nemotron_ultra": ModelSpec(
                key="nemotron_ultra", id="nemotron-3-ultra-free", priority=70,
                notes="Free, tools verified working 2026-08-26.",
            ),
            "nemotron_lightning": ModelSpec(
                key="nemotron_lightning", id="nemotron-3.5-lightning-free",
                priority=60, notes="Fast free route (~750ms observed).",
            ),
            "laguna": ModelSpec(
                key="laguna", id="laguna-s-2.1-free", priority=55,
                notes="Fast free route (~675ms observed).",
            ),
            "hy3": ModelSpec(
                key="hy3", id="hy3-free", priority=50, notes="Free route.",
            ),
        },
    )

    openrouter = ProviderAdapter(
        name="openrouter",
        base_url=OPENROUTER_BASE,
        role=ProviderRole.OX_FALLBACK,
        api_key_env="OPENROUTER_API_KEY",
        limiter=NullLimiter("openrouter"),
        requires_key=True,
        timeout_s=180.0,
        models={
            "ox_alpha": ModelSpec(
                key="ox_alpha", id="stealth/ox-alpha", priority=80,
                ephemeral_preview=True,
                notes="Alternate Ox Alpha route. Verify before relying on it.",
            ),
        },
    )

    nim = ProviderAdapter(
        name="nvidia_nim",
        base_url=NVIDIA_NIM_BASE,
        role=ProviderRole.SPECIALIST_AND_FALLBACK,
        api_key_env="NVIDIA_API_KEY",
        # The one provider with a stated contract: 48 RPM. Internal hard cap 44,
        # normal target 42. Shared globally across every worker and retry.
        limiter=RateLimiter(
            "nvidia_nim", hard_cap_per_window=nim_hard_cap,
            window_seconds=60.0, target_rpm=nim_target_rpm,
            burst_capacity=nim_burst,
            # Persist the rolling window so a broker restart cannot forget it
            # and immediately burst past the contract — the moment a restart is
            # most likely is a crash loop under load.
            state_path=nim_state_path or os.environ.get(
                "OE_MAX_NIM_STATE", os.path.join(".evolution", "nim.window")),
        ),
        requires_key=True,
        timeout_s=120.0,
        # Deliberately empty: NIM model IDs must be discovered live, not
        # remembered. `discover` populates this.
        models={},
    )

    return {p.name: p for p in (zen, openrouter, nim)}


class Registry:
    """Holds providers, discovers models, and records what actually works."""

    def __init__(self, providers: Optional[Dict[str, ProviderAdapter]] = None) -> None:
        self.providers: Dict[str, ProviderAdapter] = (
            providers if providers is not None else build_default_registry()
        )
        self.discovered: Dict[str, List[str]] = {}
        self.probes: List[ProbeResult] = []
        self.discovered_at: Optional[float] = None

    def provider(self, name: str) -> Optional[ProviderAdapter]:
        return self.providers.get(name)

    def usable_providers(self) -> List[ProviderAdapter]:
        return [p for p in self.providers.values() if p.usable()]

    # -- discovery -----------------------------------------------------

    async def discover(self, client: httpx.AsyncClient) -> Dict[str, List[str]]:
        """Stage 1: what does each provider claim to offer?"""
        out: Dict[str, List[str]] = {}
        for p in self.providers.values():
            if not p.usable():
                out[p.name] = []
                continue
            try:
                out[p.name] = await p.list_models(client)
            except Exception as exc:
                out[p.name] = []
                self.probes.append(ProbeResult(
                    p.name, "<listing>", False, None, None, None,
                    f"model listing failed: {type(exc).__name__}: {exc}"[:200],
                ))
        self.discovered = out
        self.discovered_at = time.time()
        return out

    async def probe_model(
        self, client: httpx.AsyncClient, provider: ProviderAdapter,
        model_id: str, *, check_tools: bool = True,
    ) -> ProbeResult:
        """Stage 2: does it actually serve? Listing is not proof."""
        r = await provider.chat(
            client, model_id,
            [{"role": "user", "content": "Reply with exactly: ok"}],
            max_tokens=200, temperature=0,
        )
        if not r.ok:
            return ProbeResult(provider.name, model_id, False, None,
                               r.latency_ms, r.status_code, (r.error or "")[:200])

        supports_tools: Optional[bool] = None
        if check_tools:
            t = await provider.chat(
                client, model_id,
                [{"role": "user", "content": "Reply with exactly: ok"}],
                max_tokens=200, temperature=0,
                tools=[{"type": "function", "function": {
                    "name": "noop", "description": "probe",
                    "parameters": {"type": "object", "properties": {}}}}],
            )
            supports_tools = t.ok

        return ProbeResult(provider.name, model_id, True, supports_tools,
                           r.latency_ms, r.status_code, "ok")

    async def verify(
        self, client: httpx.AsyncClient, *, check_tools: bool = True,
    ) -> List[ProbeResult]:
        """Smoke-test every configured model and record the truth on the spec."""
        results: List[ProbeResult] = []
        for p in self.providers.values():
            if not p.usable():
                results.append(ProbeResult(
                    p.name, "<provider>", False, None, None, None,
                    f"unusable: {'missing ' + str(p.api_key_env) if p.requires_key and not p.has_key else 'disabled'}",
                ))
                continue
            for spec in p.models.values():
                res = await self.probe_model(client, p, spec.id, check_tools=check_tools)
                # Belief is replaced by measurement.
                spec.available = res.reachable
                spec.supports_tools = res.supports_tools
                spec.observed_latency_ms = res.latency_ms
                results.append(res)
        self.probes = results
        return results

    def snapshot(self) -> Dict[str, Any]:
        return {
            "providers": {n: p.to_dict() for n, p in self.providers.items()},
            "discovered": self.discovered,
            "discovered_at": self.discovered_at,
            "probes": [p.to_dict() for p in self.probes],
        }
