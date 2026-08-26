"""
Provider and model profiles.

Default policy (SOURCE_OF_TRUTH section 16.1): the strongest currently-free
OpenCode Zen route is the PREFERRED primary; NVIDIA NIM is the strong fallback.
The policy is stated in terms of a *property* rather than a model name, and this
file is the reason why.

**Rewritten 2026-08-26 after four of the five configured remote routes turned
out to be dead at the same time.** What was measured that day, keylessly, with
three repeats per model:

    zen-ox-alpha-free   x-preview-f-free                 HTTP 401 "not supported",
                                                         and absent from Zen's
                                                         catalogue entirely
    zen-deepseek-v4-flash                                HTTP 401 "Missing API key"
                                                         — it was configured as a
                                                         keyless route; it is paid
    nim-deepseek-v4-pro deepseek-ai/deepseek-v4-pro      absent from NIM's catalogue
    nim-qwen25-coder-32b qwen/qwen2.5-coder-32b-instruct absent from NIM's catalogue
                                                         (NIM lists no qwen at all)
    zen-nemotron-3-ultra-free                            healthy: 3/3 chat, 3/3 tools

Only the last one worked. Every chain in the old table led with Ox Alpha, so the
default configuration routed every role to a model that had been withdrawn.

Three things changed as a result, and each is a guard rather than a correction:

  1. `catalog.py` reconciles every configured id against the provider's live
     `/models` listing on each doctor run, so the next withdrawal is diagnosed
     in one sentence instead of read out of an HTTP status. Both providers serve
     that listing without a credential, which is what makes it cheap.

  2. Capabilities are declared from measurement, and the doctor now requires an
     actual `tool_calls` entry — over two attempts, not one — before recording
     TOOLS. `zen-laguna-s-2.1-free` emits a tool call on roughly one attempt in
     three; probed once it would promote itself into every agent role a third of
     the time.

  3. Free status stays three-valued and defaults to UNKNOWN. Zen's free replies
     carry `cost: "0"`, which is good evidence they are free *now* and none at
     all that they will stay free. Section 35 acceptance criterion 27 forbids the
     UI claiming permanent free access; Ox Alpha is why that criterion exists,
     and its withdrawal is why it should be kept.

The other standing lesson is that routing must be capability-aware as well as
health-aware. A route can be healthy, preferred, and unable to serve a tools
request — Ox Alpha was exactly that under anomalyco/opencode#44300, and Laguna is
that today. Health alone would keep routing agent work to it and every agent run
would fail.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class Capability(str, Enum):
    CHAT = "chat"
    TOOLS = "tools"          # function calling — required by OpenCode/OMO agents
    JSON_MODE = "json_mode"
    VISION = "vision"
    STREAMING = "streaming"


class FreeStatus(str, Enum):
    """
    Deliberately three-valued.

    UNKNOWN is the default and is *not* rendered as "free" anywhere. Section 35
    acceptance criterion 27 forbids the UI claiming permanent free access, so a
    value we have not probed must not silently read as free.
    """

    FREE_LIMITED_TIME = "free_limited_time"
    FREE = "free"
    PAID = "paid"
    UNKNOWN = "unknown"


class Role(str, Enum):
    """Task roles the router assigns models to (SOURCE_OF_TRUTH section 16.1)."""

    MUTATION = "mutation"              # OpenEvolve mutation/reasoning
    ORCHESTRATOR = "orchestrator"      # Sisyphus / primary agent
    DEEP_CODING = "deep_coding"        # Hephaestus
    PLANNING = "planning"              # Prometheus / Hyperplan
    REVIEW = "review"                  # Atlas
    ARCHITECTURE = "architecture"      # Oracle
    RESEARCH = "research"              # Librarian
    EXPLORE = "explore"                # codebase scanning
    PARALLEL_WORKER = "parallel_worker"
    EVALUATOR = "evaluator"            # LLM-as-judge evaluation
    EMERGENCY = "emergency"


# Roles that drive an agent harness and therefore REQUIRE working tool calls.
TOOL_REQUIRING_ROLES = frozenset(
    {
        Role.ORCHESTRATOR,
        Role.DEEP_CODING,
        Role.PLANNING,
        Role.REVIEW,
        Role.ARCHITECTURE,
        Role.EXPLORE,
    }
)


@dataclass
class ModelProfile:
    """One concrete model on one provider."""

    id: str                      # stable local handle
    provider: str
    model: str                   # wire model id sent to the API
    api_base: str
    secret_ref: Optional[str] = None   # env var NAME, never the value

    display_name: str = ""
    context_limit: Optional[int] = None
    max_output_tokens: Optional[int] = None

    # Capabilities we *believe* are present; the doctor replaces belief with
    # measurement and writes the result into `verified_capabilities`.
    declared_capabilities: List[Capability] = field(
        default_factory=lambda: [Capability.CHAT]
    )
    verified_capabilities: Optional[List[Capability]] = None

    free_status: FreeStatus = FreeStatus.UNKNOWN
    free_note: str = ""

    # Routing knobs
    priority: int = 100          # lower wins
    max_concurrency: int = 4
    rpm: Optional[int] = None
    tpm: Optional[int] = None
    timeout_s: float = 120.0
    max_retries: int = 3
    backoff_base_s: float = 1.0
    temperature: Optional[float] = None
    top_p: Optional[float] = None

    # Cost metadata. None means "unknown", which the UI renders as unknown
    # rather than as zero — a fabricated 0.0 would understate real spend.
    input_cost_per_mtok: Optional[float] = None
    output_cost_per_mtok: Optional[float] = None
    cost_basis: str = ""

    # Filled in by the provider doctor's catalogue reconciliation. Purely
    # diagnostic: `absent` never disables a route, because an unlisted model can
    # still serve — Ox Alpha did for weeks. See `catalog.py`.
    catalog_status: str = "unknown"
    catalog_detail: str = ""
    catalog_suggestions: List[str] = field(default_factory=list)

    # The last live verdict from the provider doctor. None means never probed.
    #
    # This exists because a doctor run used to be able to establish that a route
    # was dead and change nothing: `apply_reports` only ever *added* verified
    # capabilities, so a model that failed its chat probe outright kept its
    # declared capabilities and kept leading its chain. On 2026-08-26 the doctor
    # measured `zen-laguna-s-2.1-free` returning HTTP 503 and the router went on
    # selecting it for two roles. The circuit breaker would have caught it, but
    # only after spending real requests to rediscover what had just been
    # measured for free.
    #
    # Deliberately expiring rather than sticky — see `probe_is_fresh`. A route
    # that recovers must be able to come back without an operator noticing.
    last_probe_ok: Optional[bool] = None
    last_probe_at: float = 0.0
    last_probe_detail: str = ""

    roles: List[Role] = field(default_factory=list)
    enabled: bool = True
    # Whether the route is unusable without its credential. OpenCode Zen was
    # observed serving `x-preview-f-free` with no Authorization header at all,
    # so treating a missing key as disqualifying would switch off a working
    # primary route.
    requires_key: bool = True
    notes: str = ""

    def capabilities(self) -> List[Capability]:
        """Measured capabilities when we have them, declared otherwise."""
        return (
            self.verified_capabilities
            if self.verified_capabilities is not None
            else self.declared_capabilities
        )

    def supports(self, cap: Capability) -> bool:
        return cap in self.capabilities()

    def has_secret(self) -> bool:
        return bool(self.secret_ref and os.environ.get(self.secret_ref))

    def usable(self) -> bool:
        """Attemptable at all: enabled, and credentialled if it needs to be."""
        if not self.enabled:
            return False
        return self.has_secret() or not self.requires_key

    def probe_is_fresh(self, ttl_s: float = 600.0) -> bool:
        """
        Whether the last doctor verdict is recent enough to route on.

        Bounded on purpose. A failed probe suppresses the route, and a
        suppression that never expires is indistinguishable from deleting the
        model: a provider blip at 09:00 would keep a healthy route off the table
        all day. After the TTL the route is simply unproven again and competes
        on its declared capabilities, where the circuit breaker can judge it on
        live traffic.
        """
        if self.last_probe_ok is None or not self.last_probe_at:
            return False
        return (time.time() - self.last_probe_at) <= ttl_s

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "model": self.model,
            "api_base": self.api_base,
            "display_name": self.display_name or self.model,
            "secret_ref": self.secret_ref,
            "secret_present": self.has_secret(),
            "requires_key": self.requires_key,
            "usable": self.usable(),
            "context_limit": self.context_limit,
            "max_output_tokens": self.max_output_tokens,
            "declared_capabilities": [c.value for c in self.declared_capabilities],
            "verified_capabilities": (
                [c.value for c in self.verified_capabilities]
                if self.verified_capabilities is not None else None
            ),
            "free_status": self.free_status.value,
            "free_note": self.free_note,
            "priority": self.priority,
            "max_concurrency": self.max_concurrency,
            "rpm": self.rpm,
            "timeout_s": self.timeout_s,
            "input_cost_per_mtok": self.input_cost_per_mtok,
            "output_cost_per_mtok": self.output_cost_per_mtok,
            "cost_basis": self.cost_basis,
            "roles": [r.value for r in self.roles],
            "enabled": self.enabled,
            "notes": self.notes,
            "catalog_status": self.catalog_status,
            "catalog_detail": self.catalog_detail,
            "catalog_suggestions": list(self.catalog_suggestions),
            "last_probe_ok": self.last_probe_ok,
            "last_probe_at": self.last_probe_at or None,
            "last_probe_detail": self.last_probe_detail,
        }


# --------------------------------------------------------------------------
# Verified defaults
# --------------------------------------------------------------------------

OPENCODE_ZEN_BASE = "https://opencode.ai/zen/v1"
NVIDIA_NIM_BASE = "https://integrate.api.nvidia.com/v1"
OPENROUTER_BASE = "https://openrouter.ai/api/v1"

_MEASURED = "measured live 2026-08-26, 3 repeats, no Authorization header"
_ZEN_SOURCE = "opencode.ai/zen/v1/models, read 2026-08-26"
_NIM_CATALOG = "integrate.api.nvidia.com/v1/models, read keylessly 2026-08-26"
_UNSERVED = (
    "Model id confirmed present in the provider's catalogue on 2026-08-26, but "
    "NOT verified serving: no credential was available in this repo. Treat "
    "every capability below as declared, not measured. The provider doctor "
    "will replace it with a measurement the first time it runs with a key."
)

_OX_ALPHA_GONE = (
    "WITHDRAWN. `x-preview-f-free` is absent from Zen's catalogue as of "
    "2026-08-26 and a direct request returns HTTP 401 "
    "\"Model x-preview-f-free is not supported\". It was documented as free "
    "\"for a limited time\" and served unlisted as a stealth preview; the "
    "limited time is up. Kept here, disabled, rather than deleted, so the "
    "Models page can say what happened to the operator's preferred route "
    "instead of silently dropping it — and so that re-enabling it is one flag "
    "if it returns."
)


def default_profiles() -> List[ModelProfile]:
    """
    The shipped default routing table.

    Every OpenCode Zen entry below that is enabled and keyless was probed live
    on 2026-08-26 with three chat attempts and three tools attempts. Entries
    that need a credential this repo does not hold carry `_UNSERVED` and declare
    rather than claim their capabilities.
    """
    return [
        # ================= OPENCODE ZEN — free tier, keyless ==============
        # Primary. The largest free model on the route (550B/55B active) and
        # the only one with a prior track record here: BENCHMARKS recorded it
        # at 100% success and 112 s p50 when Ox Alpha was at 26% and 284 s.
        # Re-measured 2026-08-26 at 4.0 s p50, so it is now both the strongest
        # free route and a fast one.
        ModelProfile(
            id="zen-nemotron-3-ultra-free",
            provider="opencode_zen",
            model="nemotron-3-ultra-free",
            api_base=OPENCODE_ZEN_BASE,
            secret_ref="OPENCODE_API_KEY",
            display_name="Nemotron 3 Ultra Free (OpenCode Zen)",
            declared_capabilities=[
                Capability.CHAT, Capability.TOOLS, Capability.JSON_MODE,
                Capability.STREAMING,
            ],
            free_status=FreeStatus.FREE_LIMITED_TIME,
            free_note=(
                "Responses carry `cost: \"0\"`. Zen's free tier is not documented "
                f"as permanent and models have been withdrawn without notice — "
                f"see the Ox Alpha entry. Source: {_ZEN_SOURCE}."
            ),
            requires_key=False,
            priority=0,
            max_concurrency=3,
            input_cost_per_mtok=0.0,
            output_cost_per_mtok=0.0,
            cost_basis=f"response body reported cost \"0\" — {_MEASURED}",
            roles=[
                Role.MUTATION, Role.EVALUATOR, Role.RESEARCH, Role.PARALLEL_WORKER,
                Role.ORCHESTRATOR, Role.DEEP_CODING, Role.PLANNING, Role.REVIEW,
                Role.ARCHITECTURE, Role.EXPLORE,
            ],
            notes=(
                f"chat 3/3 at 4.04 s p50; tools 3/3 emitting real tool_calls at "
                f"7.13 s p50 ({_MEASURED}). Caveat worth carrying: an earlier "
                f"single probe of this model at max_tokens=256 returned HTTP 200 "
                f"with an empty message and no tool call — that was truncation "
                f"by hidden reasoning, not a broken route, and it is why the "
                f"doctor's probe budget is no longer 16 tokens."
            ),
        ),
        # Fastest verified tools route. Answers a tool call in ~2.9 s, which is
        # 2.5x quicker than the primary, on a smaller model.
        ModelProfile(
            id="zen-hy3-free",
            provider="opencode_zen",
            model="hy3-free",
            api_base=OPENCODE_ZEN_BASE,
            secret_ref="OPENCODE_API_KEY",
            display_name="HY3 Free (OpenCode Zen)",
            declared_capabilities=[
                Capability.CHAT, Capability.TOOLS, Capability.STREAMING,
            ],
            free_status=FreeStatus.FREE_LIMITED_TIME,
            free_note=(
                "Responses carry `cost: \"0\"`. Free status is not documented as "
                f"permanent. Source: {_ZEN_SOURCE}."
            ),
            requires_key=False,
            priority=5,
            max_concurrency=4,
            input_cost_per_mtok=0.0,
            output_cost_per_mtok=0.0,
            cost_basis=f"response body reported cost \"0\" — {_MEASURED}",
            roles=[
                Role.EXPLORE, Role.RESEARCH, Role.PARALLEL_WORKER, Role.EVALUATOR,
                Role.ORCHESTRATOR, Role.DEEP_CODING, Role.PLANNING, Role.REVIEW,
                Role.ARCHITECTURE, Role.MUTATION,
            ],
            notes=(
                f"chat 3/3 at 2.34 s p50; tools 3/3 emitting real tool_calls at "
                f"2.86 s p50 ({_MEASURED}). Cheapest verified tools route by "
                f"wall-clock; spends 13-60 tokens on reasoning."
            ),
        ),
        # Fastest chat route, and the only one that spends zero tokens on hidden
        # reasoning — which is why it leads the latency-sensitive roles. Its
        # tools support is NOT declared: see the note.
        ModelProfile(
            id="zen-laguna-s-2.1-free",
            provider="opencode_zen",
            model="laguna-s-2.1-free",
            api_base=OPENCODE_ZEN_BASE,
            secret_ref="OPENCODE_API_KEY",
            display_name="Laguna S 2.1 Free (OpenCode Zen)",
            context_limit=262_144,
            declared_capabilities=[Capability.CHAT, Capability.STREAMING],
            free_status=FreeStatus.FREE_LIMITED_TIME,
            free_note=(
                "Responses carry `cost: \"0\"`. Free status is not documented as "
                f"permanent. Source: {_ZEN_SOURCE}."
            ),
            requires_key=False,
            priority=10,
            max_concurrency=6,
            input_cost_per_mtok=0.0,
            output_cost_per_mtok=0.0,
            cost_basis=f"response body reported cost \"0\" — {_MEASURED}",
            roles=[Role.PARALLEL_WORKER, Role.RESEARCH, Role.MUTATION, Role.EVALUATOR],
            notes=(
                f"chat 8/10 at 1.74 s p50 with 0 reasoning tokens — the fastest "
                f"route measured, and the least reliable of the four: the other "
                f"2 attempts returned HTTP 503 \"Endpoint is unavailable\". "
                f"Second in the latency-sensitive chains rather than first, "
                f"because hy3 is 100% reliable for 0.6 s more. "
                f"TOOLS deliberately NOT declared: only 1 of 3 "
                f"tools attempts emitted a tool call, the other two failing with "
                f"\"Upstream request failed: Endpoint is unavailable\" — the same "
                f"error Ox Alpha's tool bug produced. A capability that works a "
                f"third of the time fails the agent run that needed it. The "
                f"doctor probes tools twice and requires both, so this will not "
                f"promote itself on a lucky draw; if the route is fixed, two "
                f"clean probes promote it with no code change."
            ),
        ),
        ModelProfile(
            id="zen-nemotron-35-lightning-free",
            provider="opencode_zen",
            model="nemotron-3.5-lightning-free",
            api_base=OPENCODE_ZEN_BASE,
            secret_ref="OPENCODE_API_KEY",
            display_name="Nemotron 3.5 Lightning Free (OpenCode Zen)",
            declared_capabilities=[
                Capability.CHAT, Capability.TOOLS, Capability.STREAMING,
            ],
            free_status=FreeStatus.FREE_LIMITED_TIME,
            free_note=(
                "Responses carry `cost: \"0\"`. Free status is not documented as "
                f"permanent. Source: {_ZEN_SOURCE}."
            ),
            requires_key=False,
            priority=20,
            max_concurrency=4,
            input_cost_per_mtok=0.0,
            output_cost_per_mtok=0.0,
            cost_basis=f"response body reported cost \"0\" — {_MEASURED}",
            roles=[
                Role.PARALLEL_WORKER, Role.RESEARCH, Role.EXPLORE, Role.MUTATION,
                Role.ORCHESTRATOR, Role.DEEP_CODING, Role.PLANNING, Role.REVIEW,
                Role.ARCHITECTURE,
            ],
            notes=(
                f"chat 3/3 at 2.82 s p50; tools 3/3 emitting real tool_calls at "
                f"10.41 s p50 ({_MEASURED}). Despite the name it is the slowest "
                f"verified route on tool calls — it spends ~120 tokens on hidden "
                f"reasoning per reply. Fourth in the tools chain for that reason."
            ),
        ),
        # ---------- Zen free tier, rate limited for anonymous callers ----------
        # Both of these are in Zen's catalogue and both refused every anonymous
        # attempt with `FreeUsageLimitError`. That is a shared-pool limit, not a
        # dead model, so they are configured rather than deleted — but they are
        # marked as needing a key, because a keyless caller demonstrably cannot
        # use them. Whether a key actually lifts the limit is UNVERIFIED.
        ModelProfile(
            id="zen-mimo-v2.5-free",
            provider="opencode_zen",
            model="mimo-v2.5-free",
            api_base=OPENCODE_ZEN_BASE,
            secret_ref="OPENCODE_API_KEY",
            display_name="MiMo v2.5 Free (OpenCode Zen)",
            declared_capabilities=[Capability.CHAT, Capability.STREAMING],
            free_status=FreeStatus.UNKNOWN,
            free_note="Listed on Zen's free tier; never served a probe, so unprobed.",
            requires_key=True,
            priority=40,
            max_concurrency=2,
            roles=[Role.PARALLEL_WORKER, Role.RESEARCH],
            notes=(
                f"0/3 anonymous attempts served: HTTP 429 `FreeUsageLimitError` "
                f"every time ({_MEASURED}). In the catalogue, so the model "
                f"exists. Requires a key here on the evidence that keyless "
                f"access does not work — NOT on evidence that a key does."
            ),
        ),
        ModelProfile(
            id="zen-big-pickle",
            provider="opencode_zen",
            model="big-pickle",
            api_base=OPENCODE_ZEN_BASE,
            secret_ref="OPENCODE_API_KEY",
            display_name="Big Pickle (OpenCode Zen, unnamed preview)",
            declared_capabilities=[Capability.CHAT, Capability.STREAMING],
            free_status=FreeStatus.UNKNOWN,
            free_note="Unnamed preview; pricing not published and not probed.",
            requires_key=True,
            priority=45,
            max_concurrency=2,
            roles=[Role.RESEARCH],
            notes=(
                f"0/3 anonymous attempts served: HTTP 429 `FreeUsageLimitError` "
                f"every time ({_MEASURED}). Codename-style id with no published "
                f"card — the same shape Ox Alpha had, so it may be the current "
                f"stealth preview. Unverified in every respect."
            ),
        ),
        # ---------------- Zen, paid tier ----------------
        ModelProfile(
            id="zen-deepseek-v4-flash",
            provider="opencode_zen",
            model="deepseek-v4-flash",
            api_base=OPENCODE_ZEN_BASE,
            secret_ref="OPENCODE_API_KEY",
            display_name="DeepSeek v4 Flash (OpenCode Zen, paid)",
            declared_capabilities=[
                Capability.CHAT, Capability.TOOLS, Capability.STREAMING,
            ],
            free_status=FreeStatus.PAID,
            free_note="Zen's paid tier: a keyless request is refused with `Missing API key.`",
            # Corrected 2026-08-26. This was configured keyless, which was wrong:
            # the keyless request returns HTTP 401 `AuthError: Missing API key.`
            # The free-tier models above are the keyless ones.
            requires_key=True,
            priority=50,
            max_concurrency=6,
            roles=[Role.EXPLORE, Role.RESEARCH, Role.PARALLEL_WORKER],
            notes=(
                f"In Zen's catalogue. Refused keyless access with HTTP 401 "
                f"`Missing API key.` ({_MEASURED}) — so it is a paid route, and "
                f"the previous keyless configuration was a bug. Note the "
                f"separate `deepseek-v4-flash-free` id is in the catalogue too "
                f"and answers HTTP 400 `Model is unavailable`, which is the "
                f"standing example of why being listed is not being served."
            ),
        ),
        # ---------------- WITHDRAWN ----------------
        ModelProfile(
            id="zen-ox-alpha-free",
            provider="opencode_zen",
            model="x-preview-f-free",
            api_base=OPENCODE_ZEN_BASE,
            secret_ref="OPENCODE_API_KEY",
            display_name="Ox Alpha Free (OpenCode Zen) — withdrawn",
            context_limit=1_048_576,
            max_output_tokens=131_072,
            declared_capabilities=[Capability.CHAT],
            free_status=FreeStatus.UNKNOWN,
            free_note="Was documented free for a limited time. The model is gone.",
            requires_key=False,
            priority=900,
            enabled=False,
            roles=[],
            notes=_OX_ALPHA_GONE,
        ),
        # ================= NVIDIA NIM — catalogue-checked, unserved =========
        # Both previously configured NIM ids (`deepseek-ai/deepseek-v4-pro` and
        # `qwen/qwen2.5-coder-32b-instruct`) were absent from NIM's live
        # catalogue on 2026-08-26 — NIM lists no qwen model at all now. The ids
        # below were taken FROM that catalogue rather than from memory, which is
        # the whole reason `catalog.py` exists.
        ModelProfile(
            id="nim-nemotron-3-ultra-550b",
            provider="nvidia_nim",
            model="nvidia/nemotron-3-ultra-550b-a55b",
            api_base=NVIDIA_NIM_BASE,
            secret_ref="NVIDIA_API_KEY",
            display_name="Nemotron 3 Ultra 550B (NVIDIA NIM)",
            declared_capabilities=[
                Capability.CHAT, Capability.TOOLS, Capability.STREAMING,
            ],
            free_status=FreeStatus.UNKNOWN,
            free_note="NIM credit terms depend on the account; probe at runtime.",
            priority=60,
            max_concurrency=4,
            roles=[
                Role.EMERGENCY, Role.MUTATION, Role.DEEP_CODING, Role.ORCHESTRATOR,
                Role.PLANNING, Role.REVIEW, Role.ARCHITECTURE,
            ],
            notes=f"Present in {_NIM_CATALOG}. {_UNSERVED}",
        ),
        ModelProfile(
            id="nim-gpt-oss-120b",
            provider="nvidia_nim",
            model="openai/gpt-oss-120b",
            api_base=NVIDIA_NIM_BASE,
            secret_ref="NVIDIA_API_KEY",
            display_name="GPT-OSS 120B (NVIDIA NIM)",
            declared_capabilities=[
                Capability.CHAT, Capability.TOOLS, Capability.STREAMING,
            ],
            free_status=FreeStatus.UNKNOWN,
            priority=70,
            max_concurrency=4,
            roles=[Role.EMERGENCY, Role.MUTATION, Role.DEEP_CODING, Role.EVALUATOR],
            notes=f"Present in {_NIM_CATALOG}. {_UNSERVED}",
        ),
        ModelProfile(
            id="nim-nemotron-3-super-120b",
            provider="nvidia_nim",
            model="nvidia/nemotron-3-super-120b-a12b",
            api_base=NVIDIA_NIM_BASE,
            secret_ref="NVIDIA_API_KEY",
            display_name="Nemotron 3 Super 120B (NVIDIA NIM)",
            declared_capabilities=[Capability.CHAT, Capability.STREAMING],
            free_status=FreeStatus.UNKNOWN,
            priority=80,
            max_concurrency=4,
            roles=[Role.MUTATION, Role.PARALLEL_WORKER, Role.EVALUATOR, Role.EMERGENCY],
            notes=f"Present in {_NIM_CATALOG}. {_UNSERVED}",
        ),
        # ================= OPENROUTER — catalogue-checked, unserved =========
        # OpenRouter's catalogue reads without a credential and publishes per
        # model pricing, so a $0 model can be identified before any request is
        # made. 25 of its 416 models priced at $0 on 2026-08-26. Serving is
        # still unverified: OpenRouter requires a key for completions.
        ModelProfile(
            id="openrouter-nemotron-3-ultra-free",
            provider="openrouter",
            model="nvidia/nemotron-3-ultra-550b-a55b:free",
            api_base=OPENROUTER_BASE,
            secret_ref="OPENROUTER_API_KEY",
            display_name="Nemotron 3 Ultra 550B free (OpenRouter)",
            context_limit=1_000_000,
            declared_capabilities=[
                Capability.CHAT, Capability.TOOLS, Capability.STREAMING,
            ],
            free_status=FreeStatus.FREE_LIMITED_TIME,
            free_note=(
                "OpenRouter lists prompt and completion pricing at $0 for this "
                "id (read keylessly 2026-08-26). `:free` variants carry their "
                "own daily caps and OpenRouter withdraws them without notice, "
                "so this is not permanent free access."
            ),
            input_cost_per_mtok=0.0,
            output_cost_per_mtok=0.0,
            cost_basis="OpenRouter /models pricing, read keylessly 2026-08-26",
            priority=85,
            max_concurrency=2,
            roles=[Role.MUTATION, Role.PARALLEL_WORKER, Role.RESEARCH, Role.EMERGENCY],
            notes=(
                f"A second, independent free route to the same model family as "
                f"the primary — useful precisely because its rate limit is a "
                f"different bucket from Zen's. {_UNSERVED}"
            ),
        ),
        # ---------------- LOCAL / OPENAI-COMPATIBLE ----------------
        ModelProfile(
            id="local-openai-compatible",
            provider="local",
            model=os.environ.get("EVOLUTION_LOCAL_MODEL", "local-model"),
            api_base=os.environ.get("EVOLUTION_LOCAL_BASE", "http://127.0.0.1:11434/v1"),
            secret_ref=None,
            display_name="Local OpenAI-compatible endpoint",
            declared_capabilities=[Capability.CHAT],
            free_status=FreeStatus.FREE,
            free_note="Runs on the operator's own hardware.",
            # A local server needs no credential, and `requires_key` defaults to
            # True. Without this the route stayed unusable even after the
            # operator enabled it, with the exclusion reason "missing credential
            # None".
            requires_key=False,
            priority=95,
            max_concurrency=2,
            roles=[Role.EMERGENCY, Role.PARALLEL_WORKER],
            enabled=False,   # opt-in: only useful if such a server is running
            notes="Enable in settings when a local server (Ollama, vLLM, LM Studio) is up.",
        ),
    ]


def default_role_chains() -> Dict[Role, List[str]]:
    """
    Ordered fallback chain per role.

    Rebuilt 2026-08-26 on measurement, because the previous chains led every
    role with `zen-ox-alpha-free` and that model no longer exists. Every chain
    now leads with a route that answered a live probe.

    The ordering principle, unchanged: chain position is a *stated preference*
    and does not self-correct, while the capability filter does. If
    `zen-hy3-free`'s tool support regresses, the next doctor run records
    `supports_tools=False` and it drops out of the agent roles on its own. If it
    becomes the best route, promoting it is still a deliberate edit.

    Measured p50s behind the ordering (2026-08-26, 3 repeats, keyless):

        route                          chat    tools   tool_calls
        zen-laguna-s-2.1-free          1.55 s  2.27 s  1/3   <- chat only
        zen-hy3-free                   2.34 s  2.86 s  3/3
        zen-nemotron-35-lightning-free 2.82 s  10.41 s 3/3
        zen-nemotron-3-ultra-free      4.04 s  7.13 s  3/3

    Completion roles lead with the largest model rather than the fastest. That
    is the operator's standing preference — strongest free route first — and
    latency alone is not grounds to change it. Whether Laguna's 2.6x speed
    advantage buys more improvement per second than Ultra's extra capacity is
    exactly the question NEXT_TASKS T1 exists to answer, now against two routes
    that both work.
    """
    # Strongest free route first, then the fast ones, then keyed fallbacks.
    completion_chain = [
        "zen-nemotron-3-ultra-free",
        "zen-hy3-free",
        "zen-laguna-s-2.1-free",
        "zen-nemotron-35-lightning-free",
        "nim-nemotron-3-ultra-550b",
        "openrouter-nemotron-3-ultra-free",
    ]
    # Only routes that emitted a real tool call on every probe. Laguna is
    # absent by measurement, not by policy.
    tools_chain = [
        "zen-nemotron-3-ultra-free",
        "zen-hy3-free",
        "zen-nemotron-35-lightning-free",
        "nim-nemotron-3-ultra-550b",
        "nim-gpt-oss-120b",
    ]
    # Cheap, high-volume, latency-dominated work. Not simply "fastest first":
    # Laguna is the quickest route measured (1.74 s p50) and serves 8 requests
    # in 10, while hy3 served 3 of 3 for 0.6 s more. One failure in five costs a
    # retry, which is worth more than 0.6 s, so reliability leads and the fast
    # route sits behind it where the health score can promote it if it steadies.
    fast_chain = [
        "zen-hy3-free",
        "zen-laguna-s-2.1-free",
        "zen-nemotron-35-lightning-free",
        "zen-nemotron-3-ultra-free",
        "openrouter-nemotron-3-ultra-free",
    ]
    return {
        Role.MUTATION: completion_chain,
        Role.EVALUATOR: completion_chain,
        Role.RESEARCH: fast_chain,
        Role.PARALLEL_WORKER: fast_chain,
        Role.ORCHESTRATOR: tools_chain,
        Role.DEEP_CODING: tools_chain,
        Role.PLANNING: tools_chain,
        Role.REVIEW: tools_chain,
        Role.ARCHITECTURE: tools_chain,
        # Explore is tool-requiring but low-value per call: cheapest verified
        # tools route first.
        Role.EXPLORE: ["zen-hy3-free"] + tools_chain,
        Role.EMERGENCY: [
            "nim-nemotron-3-ultra-550b", "nim-gpt-oss-120b",
            "openrouter-nemotron-3-ultra-free", "zen-nemotron-3-ultra-free",
            "local-openai-compatible",
        ],
    }
