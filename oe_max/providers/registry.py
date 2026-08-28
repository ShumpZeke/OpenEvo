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

Re-probed 2026-08-26, and the primary route had **vanished**:

  * `x-preview-f-free` (Ox Alpha) is gone from Zen. It is absent from
    `/models`, and calling it returns `ModelError: Model x-preview-f-free is
    not supported` — not an auth failure, which is what a paid model returns
    (`AuthError: Missing API key`). The stealth preview the spec warned might
    disappear, disappeared.
  * Still serving keyless: `nemotron-3-ultra-free`, `laguna-s-2.1-free`,
    `hy3-free`, `nemotron-3.5-lightning-free`.
  * `muse-spark-1.2-contributor-free` is newly listed and returns HTTP 500.

That is the argument for stage 0, added here: `reconcile()` crosses the live
listing against what is configured, so a model that disappears is disabled by
the next discovery instead of by someone noticing. Ox Alpha's removal cost a
debugging session that a listing check would have answered immediately.
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
from .catalogue import build_provider, load_catalogue, materialise_models
from .local import build_local_providers, local_only

# Verified live 2026-08-26.
OPENCODE_ZEN_BASE = "https://opencode.ai/zen/v1"
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
NVIDIA_NIM_BASE = "https://integrate.api.nvidia.com/v1"


# Outcomes where the provider has told us something durable about the model:
# it does not exist, we may not call it, or our allowance for it is gone.
# Anything else — a 502, a timeout, a dropped connection — is the network or
# the provider having a bad minute, and says nothing about the model.
_DEFINITIVE = frozenset({
    Outcome.BAD_REQUEST,            # Zen's "Model is unavailable"
    Outcome.FREE_LIMIT_EXHAUSTED,   # unusable now, and not by accident
})

# A withdrawn model on Zen arrives as 401 with a ModelError body, which is
# indistinguishable from a credential problem by status alone.
_WITHDRAWN_MARKERS = ("modelerror", "not supported", "does not exist", "not found")


@dataclass
class ProbeResult:
    provider: str
    model: str
    reachable: bool
    supports_tools: Optional[bool]
    latency_ms: Optional[float]
    status: Optional[int]
    detail: str = ""
    # Whether this probe is evidence about the *model*, or just a bad minute.
    # Without the distinction one transient 502 silently removes a working
    # route from every chain until somebody re-runs verification — observed
    # live on 2026-08-26, when `laguna-s-2.1-free` failed a single probe
    # between two successful runs and would have taken the judge and fast
    # roles down with it.
    conclusive: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


def _is_conclusive(outcome: Outcome, body: str) -> bool:
    if outcome in _DEFINITIVE:
        return True
    if outcome is Outcome.AUTH_FAILED:
        low = (body or "").lower()
        return any(m in low for m in _WITHDRAWN_MARKERS)
    return False


def build_default_registry(
    *,
    nim_hard_cap: int = 44,
    nim_target_rpm: float = 42.0,
    nim_burst: float = 2.0,
    nim_state_path: Optional[str] = None,
    include_catalogue: bool = True,
    catalogue_path: Optional[str] = None,
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
            "nemotron_ultra": ModelSpec(
                key="nemotron_ultra", id="nemotron-3-ultra-free", priority=100,
                notes="Strongest verified-free Zen route. Re-probed 2026-08-26: "
                      "HTTP 200 in 3.3s keyless, reasoning tokens reported. "
                      "Promoted to primary when Ox Alpha was withdrawn.",
            ),
            "hy3": ModelSpec(
                key="hy3", id="hy3-free", priority=70,
                notes="Re-probed 2026-08-26: HTTP 200 in 2.1s keyless, "
                      "43 reasoning tokens on a trivial prompt.",
            ),
            "laguna": ModelSpec(
                key="laguna", id="laguna-s-2.1-free", priority=60,
                notes="Re-probed 2026-08-26: HTTP 200 in 1.6s keyless, and the "
                      "only free Zen route reporting ZERO reasoning tokens plus "
                      "a prompt cache hit. Cheapest route per useful token, so "
                      "it leads the fast/judge chains rather than the reasoners.",
            ),
            "nemotron_lightning": ModelSpec(
                key="nemotron_lightning", id="nemotron-3.5-lightning-free",
                priority=55,
                notes="Re-probed 2026-08-26: HTTP 200 but finish_reason=length "
                      "with 64/64 completion tokens spent on hidden reasoning. "
                      "Usable only with a large max_tokens — see HANDOFF 3.3. "
                      "Named 'lightning' but measured slowest of the four (7.6s).",
            ),
            "mimo": ModelSpec(
                key="mimo", id="mimo-v2.5-free", priority=0, available=False,
                notes="Listed but exhausted: HTTP 429 `FreeUsageLimitError` on "
                      "every attempt, 2026-08-26. Shared free pool, not a "
                      "per-account rate limit. See Outcome.FREE_LIMIT_EXHAUSTED.",
            ),
            "deepseek_flash_free": ModelSpec(
                key="deepseek_flash_free", id="deepseek-v4-flash-free",
                priority=0, available=False,
                notes="Listed but unserveable: HTTP 400 'Model is unavailable', "
                      "unchanged between 2026-08-25 and 2026-08-26. The original "
                      "evidence for two-stage discovery.",
            ),
            "muse_spark_contributor": ModelSpec(
                key="muse_spark_contributor",
                id="muse-spark-1.2-contributor-free", priority=0, available=False,
                notes="Newly listed 2026-08-26; returns HTTP 500 'Internal server "
                      "error'. The 'contributor' suffix suggests it is gated to "
                      "OpenCode contributors. Not usable.",
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
        # No models are configured here. OpenRouter carried one entry, an
        # alternate `stealth/ox-alpha` route, and the operator has taken Ox Alpha
        # out of service entirely. The provider stays declared so a key plus a
        # discovery pass can populate it, which is the same shape every other
        # catalogue provider has.
        models={},
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
        # NIM's catalogue endpoint is unauthenticated — verified 2026-08-26.
        public_listing=True,
        # Was 120s, which was a guess made before any request had been sent.
        # Measured on a real 6-iteration run 2026-08-28: the flagship averaged
        # 86s per mutation and 3 of 9 requests hit the 120s ceiling — the
        # timeouts were ours, not the provider's. A trivial probe returns in
        # 4.5s, so a limit set from probe latency would have been wrong by an
        # order of magnitude; a real mutation prompt is what has to set it.
        # See HANDOFF 3.3: this moves with max_tokens and the client timeout.
        timeout_s=420.0,
        # These were empty, on the principle that NIM model IDs must be
        # discovered live rather than remembered. The principle is right and
        # the empty table was the wrong way to honour it: it meant no chain
        # could name a NIM route, so the whole provider was unreachable except
        # by pinning a string nobody had verified.
        #
        # `reconcile()` is the better guarantee. Every id below was read out of
        # NVIDIA's live catalogue on 2026-08-26 — `GET /v1/models` needs no
        # credential, which is what makes this checkable — and any that stops
        # appearing there is disabled by the next discovery. Config names the
        # preference; the live listing remains the authority.
        #
        # VERIFIED BY INFERENCE 2026-08-28, with a real key. Four of the nine
        # ids configured from the catalogue did not serve, which makes NIM the
        # second provider to prove that a listing is not a promise:
        #
        #   nemotron-nano-3-30b-a3b   404 "Model not found" — while the
        #                             near-identical `nemotron-3-nano-30b-a3b`
        #                             works. Two transposed words, both in the
        #                             catalogue, one of them fictional in
        #                             practice. Corrected below.
        #   gpt-oss-120b              hangs: 0 bytes after 190s and again
        #                             after 230s.
        #   nemotron-3.5-lightning    400 "DEGRADED function cannot be invoked"
        #   codestral-22b             404 "Not found for account" — gated per
        #                             account, not universally available.
        #   minimax-m3                429 on every attempt, including after a
        #                             45s idle gap, so it is an allowance and
        #                             not a burst limit.
        models={
            "nemotron_ultra_253b": ModelSpec(
                key="nemotron_ultra_253b", id="nvidia/nemotron-3-ultra-550b-a55b",
                priority=100,
                notes="Flagship NIM reasoner. VERIFIED 2026-08-28: HTTP 200 in "
                      "4.5s, tools supported. Returns hidden reasoning in a "
                      "separate `reasoning_content` field rather than spending "
                      "the visible budget on it — unlike the Zen routes.",
            ),
            "gpt_oss_120b": ModelSpec(
                key="gpt_oss_120b", id="openai/gpt-oss-120b", priority=0,
                available=False,
                notes="LISTED BUT HANGS. Probed 2026-08-28: zero bytes received "
                      "after 190s, and again after 230s. Not a slow model — no "
                      "response at all. Kept disabled rather than deleted so a "
                      "later probe can re-enable it if the endpoint recovers.",
            ),
            "kimi_k3": ModelSpec(
                key="kimi_k3", id="moonshotai/kimi-k3", priority=90,
                notes="Agentic/coding strength. VERIFIED 2026-08-28: HTTP 200 "
                      "in 11.5s, tools supported.",
            ),
            "deepseek_v4_flash": ModelSpec(
                key="deepseek_v4_flash", id="deepseek-ai/deepseek-v4-flash-0731",
                priority=85,
                notes="VERIFIED 2026-08-28: HTTP 200, but slow — 51s on a "
                      "two-word prompt. Note the id is date-suffixed; "
                      "`deepseek-ai/deepseek-v4-pro`, which this project once "
                      "configured, is not in the catalogue at all.",
            ),
            "nemotron_super_120b": ModelSpec(
                key="nemotron_super_120b", id="nvidia/nemotron-3-super-120b-a12b",
                priority=98,
                notes="VERIFIED 2026-08-28: HTTP 200 in 732ms with tools — the "
                      "fastest working route measured on any provider, free or "
                      "otherwise, and six times quicker than NIM's flagship. "
                      "Promoted accordingly.",
            ),
            "minimax_m3": ModelSpec(
                key="minimax_m3", id="minimaxai/minimax-m3", priority=0,
                available=False,
                notes="LISTED, NOT SERVEABLE ON THIS ACCOUNT. HTTP 429 on every "
                      "attempt 2026-08-28, including after a 45s idle gap — an "
                      "allowance, not a burst limit.",
            ),
            "codestral": ModelSpec(
                key="codestral", id="mistralai/codestral-22b-instruct-v0.1",
                priority=0, available=False,
                notes="LISTED, ACCOUNT-GATED. HTTP 404 'Not found for account' "
                      "2026-08-28 — present in the public catalogue and not "
                      "entitled to this account, which a listing cannot express.",
            ),
            "nemotron_lightning_30b": ModelSpec(
                key="nemotron_lightning_30b",
                id="nvidia/nemotron-3.5-lightning-30b-a3b", priority=0,
                available=False,
                notes="LISTED BUT DEGRADED. HTTP 400 'DEGRADED function cannot "
                      "be invoked' 2026-08-28 — the provider's own word for it.",
            ),
            "nemotron_nano_30b": ModelSpec(
                key="nemotron_nano_30b", id="nvidia/nemotron-3-nano-30b-a3b",
                priority=85,
                notes="VERIFIED 2026-08-28. The id was `nemotron-nano-3-30b-a3b` "
                      "until a probe returned 404 'Model not found' for it: two "
                      "transposed words, BOTH present in the catalogue, only "
                      "this spelling serveable. The cleanest possible argument "
                      "against writing a model id from memory.",
            ),
        },
    )

    # Local servers are always declared. One that is not running lists nothing
    # and contributes no routes, which is the same shape as a provider whose
    # credential is absent -- so "is Ollama up?" stays a question answered by a
    # probe rather than by import-time guesswork.
    locals_ = build_local_providers()

    if local_only():
        # OE_MAX_LOCAL_ONLY is a hard guarantee, not a preference: the remote
        # providers are never constructed, so no chain entry, no refresh, no
        # catalogue file and no operator mistake can put a request on the wire
        # to a commercial endpoint. Filtering later would leave the routes
        # reachable and rely on every future code path remembering to check.
        return dict(locals_)

    providers: Dict[str, ProviderAdapter] = {
        p.name: p for p in (zen, openrouter, nim)
    }
    providers.update(locals_)

    # Catalogue providers are additive and never override a built-in: the three
    # above carry hand-checked timeouts, a rate contract and curated model
    # tables that a generic entry would flatten. Everything else an operator
    # configures joins alongside them, key-gated, so declaring fifteen
    # providers costs nothing when fourteen have no credential.
    if include_catalogue:
        cat = load_catalogue(catalogue_path)
        for entry in cat.get("providers") or []:
            if entry.name in providers:
                continue
            providers[entry.name] = build_provider(entry)

    return providers


class Registry:
    """Holds providers, discovers models, and records what actually works."""

    def __init__(self, providers: Optional[Dict[str, ProviderAdapter]] = None) -> None:
        self.providers: Dict[str, ProviderAdapter] = (
            providers if providers is not None else build_default_registry()
        )
        self.discovered: Dict[str, List[str]] = {}
        self.probes: List[ProbeResult] = []
        self.discovered_at: Optional[float] = None
        self.reconciled: Dict[str, str] = {}

    def provider(self, name: str) -> Optional[ProviderAdapter]:
        return self.providers.get(name)

    def usable_providers(self) -> List[ProviderAdapter]:
        return [p for p in self.providers.values() if p.usable()]

    # -- discovery -----------------------------------------------------

    async def discover(self, client: httpx.AsyncClient) -> Dict[str, List[str]]:
        """Stage 1: what does each provider claim to offer?"""
        out: Dict[str, List[str]] = {}
        for p in self.providers.values():
            if not p.usable() and not p.public_listing:
                out[p.name] = []
                continue
            # A provider with a public catalogue is listed even when we have no
            # credential for it. Inference still needs the key; knowing whether
            # a configured id exists does not, and that check is most valuable
            # exactly when the provider is not yet set up.
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
        # Catalogue providers arrive with no models at all; their concrete ids
        # come from the listing we just fetched, never from this file.
        for p in self.providers.values():
            if getattr(p, "prefer_patterns", None) and out.get(p.name):
                found = materialise_models(p, out[p.name])
                if found:
                    # Preserve any belief already measured about a model that
                    # is still listed — a failed smoke test must not be
                    # forgotten just because discovery ran again.
                    for key, spec in found.items():
                        old = p.models.get(key)
                        if old is not None:
                            spec.available = old.available
                            spec.supports_tools = old.supports_tools
                            spec.observed_latency_ms = old.observed_latency_ms
                    p.models = found
        # Reconciling here rather than leaving it to callers: a check that must
        # be remembered is a check that gets forgotten, which is precisely how
        # the withdrawn primary survived in the chain.
        self.reconciled = self.reconcile()
        return out

    def reconcile(self) -> Dict[str, str]:
        """
        Stage 0: cross the live listing against what is configured.

        Two-stage discovery answered "is a listed model serveable?". It never
        asked the converse — "is a configured model still listed?" — and that
        is the question that went unanswered when `x-preview-f-free` was
        withdrawn. The chain kept leading with it, every request spent its
        attempts on a model the provider had stopped acknowledging, and the
        failure surfaced as slow degradation rather than as a missing model.

        A listing is cheap, unauthenticated on both Zen and NIM, and unambiguous:
        if a model is not in it, do not spend a request finding out.

        Deliberately reversible. A model that reappears is re-enabled, because
        the listing is evidence in both directions and a permanent disable
        would need a human to undo a machine's observation.
        """
        changes: Dict[str, str] = {}
        for p in self.providers.values():
            listed = self.discovered.get(p.name)
            if not listed:
                # No listing means we failed to ask, not that the provider
                # offers nothing. Disabling every model on a transport blip
                # would be far worse than leaving belief untouched.
                continue
            listed_set = set(listed)
            for spec in p.models.values():
                label = f"{p.name}/{spec.id}"
                if spec.id not in listed_set:
                    if spec.available is not False:
                        spec.available = False
                        spec.notes = (spec.notes + " | ").lstrip(" |") + (
                            "not present in the provider's live listing")
                        changes[label] = "disabled: absent from listing"
                elif spec.available is False and "not present in the provider" in spec.notes:
                    # It came back. Only un-disable what *this* check disabled;
                    # a model turned off by a failed smoke test must stay off
                    # until a smoke test says otherwise.
                    spec.available = None
                    spec.notes = spec.notes.replace(
                        " | not present in the provider's live listing", "")
                    changes[label] = "re-listed: available pending probe"
        return changes

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
            return ProbeResult(
                provider.name, model_id, False, None, r.latency_ms,
                r.status_code, (r.error or "")[:200],
                conclusive=_is_conclusive(r.outcome, r.error or ""),
            )

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
            # The capability filter is meant to self-correct in both
            # directions: if tool support breaks, the next probe records False
            # and the model leaves tool-requiring roles with no code change.
            # That only works if a *transient* failure does not also record
            # False — otherwise one bad minute demotes a tools-capable model
            # and nothing puts it back until the next verification.
            if t.ok:
                supports_tools = True
            elif _is_conclusive(t.outcome, t.error or ""):
                supports_tools = False
            else:
                supports_tools = None   # inconclusive; keep what we knew

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
                # Belief is replaced by measurement — but only by measurement
                # that means something. An inconclusive failure leaves the
                # previous belief alone rather than overwriting it with the
                # network's opinion of the last two seconds.
                if res.reachable:
                    spec.available = True
                elif res.conclusive:
                    spec.available = False
                if res.supports_tools is not None:
                    spec.supports_tools = res.supports_tools
                if res.latency_ms is not None:
                    spec.observed_latency_ms = res.latency_ms
                results.append(res)
        self.probes = results
        return results

    def snapshot(self) -> Dict[str, Any]:
        return {
            "providers": {n: p.to_dict() for n, p in self.providers.items()},
            "discovered": self.discovered,
            "discovered_at": self.discovered_at,
            "reconciled": self.reconciled,
            "probes": [p.to_dict() for p in self.probes],
        }
