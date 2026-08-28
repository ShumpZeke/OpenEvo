"""
DEPRECATED — Legacy profiles, superseded by oe_max.brain.capabilities + policies.

New code must use BrainPort capability negotiation, not role->provider matrices.
See oe_max/brain/README.md. This file is retained only for the control plane
dashboard's historical views and will be removed after migration.

Provider and model profiles.

Default policy (SOURCE_OF_TRUTH section 16.1) named OpenCode Zen / Ox Alpha
Free the PREFERRED primary route "while it is healthy and currently free", with
NVIDIA NIM as the strong fallback.

The condition failed. Re-probed 2026-08-26, Ox Alpha is gone from Zen: absent
from `/models`, and a completion returns `ModelError: Model x-preview-f-free is
not supported` — distinguishable from gating because a paid Zen model returns
`AuthError: Missing API key` instead. The policy's own qualifier decides what
happens next, so the primary is now `nemotron-3-ultra-free`, the strongest
route measured serving keyless today.

The same re-probe found the NIM fallbacks were never real: neither
`deepseek-ai/deepseek-v4-pro` nor `qwen/qwen2.5-coder-32b-instruct` appears in
NVIDIA's live catalogue, which is public and takes one unauthenticated GET to
check. NIM hosts no Qwen model at all. Both are corrected below to ids read out
of that catalogue.

Two facts verified against current official sources on 2026-08-25 shape the
defaults below, and both are recorded rather than assumed:

  1. Ox Alpha Free is free "for a limited time" (opencode.ai/docs/zen). We
     therefore model free status as a *runtime-probed* value with an explicit
     UNKNOWN state, and the UI is forbidden from calling it unlimited. See
     `FreeStatus`.

  2. Ox Alpha currently fails for ANY request carrying a `tools` array —
     "Upstream request failed: Endpoint is unavailable" — while plain chat
     completions succeed, and other Zen models (nemotron-3-ultra-free,
     deepseek-v4-flash) handle tools fine on the same route.
     (anomalyco/opencode issue #44300, open at time of writing.)

     Consequence: routing must be *capability*-aware, not merely health-aware.
     OpenEvolve's mutation calls are plain completions, so Ox Alpha is correct
     there. OpenCode/OMO agent roles need tools, so they must fall back to a
     tools-capable model automatically. Encoding "Ox Alpha for everything"
     would produce a system that fails the moment the agent sandbox runs.
"""

from __future__ import annotations

import os
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
        }


# --------------------------------------------------------------------------
# Verified defaults
# --------------------------------------------------------------------------

OPENCODE_ZEN_BASE = "https://opencode.ai/zen/v1"
NVIDIA_NIM_BASE = "https://integrate.api.nvidia.com/v1"

_ZEN_SOURCE = "opencode.ai/docs/zen, verified 2026-08-25"
_TOOLS_ISSUE = (
    "Tool-calling was broken for this model under anomalyco/opencode issue "
    "#44300 (any request carrying a `tools` array returned 'Upstream request "
    "failed: Endpoint is unavailable'). Re-probed live 2026-08-26: RESOLVED — "
    "tools requests return 200. Capability is measured by the doctor rather "
    "than assumed, so if this regresses the model is filtered out of "
    "tool-requiring roles automatically."
)


def local_profiles() -> List[ModelProfile]:
    """
    One profile per local OpenAI-compatible server, enabled and role-complete.

    This layer is what the *agent* routes through — `ModelRouter`, not the
    broker's registry — so without it "fully local" covered evolution and left
    the tool-using agent unable to reach anything but a commercial endpoint.

    Every role is listed. On a machine running one model there is nothing to
    specialise between, and omitting a role would strand it with no route at
    all rather than with a merely imperfect one.
    """
    from oe_max.providers.local import LOCAL_SERVERS, base_url_for

    every_role = list(Role)
    out: List[ModelProfile] = []
    for name, _var, _default, label in LOCAL_SERVERS:
        out.append(ModelProfile(
            id=f"local-{name}",
            provider=name,
            # The id is resolved at request time from the server's own listing.
            # A name written here would be a guess about someone else's machine.
            model=os.environ.get("EVOLUTION_LOCAL_MODEL", ""),
            api_base=base_url_for(name),
            secret_ref=None,
            display_name=f"{label} (local)",
            # All four servers implement the OpenAI tools API; whether a given
            # *model* honours it varies, and that is what the doctor probes.
            #
            # Declaring CHAT alone was tried first and is worse: every
            # tool-requiring role (orchestrator, planning, review, architecture)
            # then has no route at all, so the agent cannot run locally under
            # any model. A declaration the doctor can withdraw from measurement
            # beats a guaranteed dead end -- and the capability filter already
            # self-corrects downward, which is exactly the case it exists for.
            declared_capabilities=[
                Capability.CHAT, Capability.TOOLS, Capability.STREAMING,
            ],
            free_status=FreeStatus.FREE,
            free_note="Runs on the operator's own hardware; no metering.",
            requires_key=False,
            priority=0,
            # One at a time: parallel requests to a single local server queue at
            # the model rather than running concurrently, and on a saturated GPU
            # they compete for the same memory.
            max_concurrency=1,
            roles=every_role,
            enabled=True,
            notes=(
                f"{label} on {base_url_for(name)}. Reachability is probed, never "
                "assumed — a server that is not running simply serves nothing."
            ),
        ))
    return out


def default_profiles() -> List[ModelProfile]:
    """
    The shipped default routing table.

    NVIDIA NIM carries the primary routes: the operator asked for NIM, and it is
    the only provider here whose models were probed individually with a real key
    (HANDOFF §4i). The keyless Zen routes remain as the fallback tail, so an
    install with no NVIDIA_API_KEY still serves rather than failing — a route
    that needs an absent credential is filtered out, not attempted.
    """
    from oe_max.providers.local import local_only

    if local_only():
        # The same guarantee the broker's registry makes, made here: the
        # commercial profiles are never constructed, so the agent's router has
        # nothing remote to select even if a role chain named it. Disabling
        # them would leave them selectable by any code path that forgot to
        # check `enabled`.
        return local_profiles()

    return [
        # ------- Zen, tools-capable (verified working per issue #44300) -------
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
                "Free on Zen and serving keyless as of 2026-08-26. Zen's free "
                f"routes carry no permanence guarantee. {_ZEN_SOURCE}"
            ),
            # Verified 2026-08-26: HTTP 200 keyless, 3.3s, reasoning reported.
            requires_key=False,
            priority=0,
            max_concurrency=3,
            input_cost_per_mtok=0.0,
            output_cost_per_mtok=0.0,
            cost_basis=f"listed $0 in/out — {_ZEN_SOURCE}",
            roles=[
                Role.MUTATION, Role.RESEARCH, Role.EVALUATOR, Role.PARALLEL_WORKER,
                Role.ORCHESTRATOR, Role.DEEP_CODING, Role.PLANNING, Role.REVIEW,
                Role.ARCHITECTURE, Role.EXPLORE,
            ],
            notes=(
                "Promoted to primary on 2026-08-26 when Ox Alpha was withdrawn. "
                "Strongest route measured serving without a credential: HTTP 200 "
                "in 3.3s, hidden-reasoning tokens reported, tools verified."
            ),
        ),
        # ---- Verified-keyless free Zen routes, measured 2026-08-26 ----
        ModelProfile(
            id="zen-laguna-s21-free",
            provider="opencode_zen",
            model="laguna-s-2.1-free",
            api_base=OPENCODE_ZEN_BASE,
            secret_ref="OPENCODE_API_KEY",
            display_name="Laguna S 2.1 Free (OpenCode Zen)",
            declared_capabilities=[
                Capability.CHAT, Capability.TOOLS, Capability.STREAMING,
            ],
            free_status=FreeStatus.FREE_LIMITED_TIME,
            free_note=f"Free on Zen, serving keyless 2026-08-26. {_ZEN_SOURCE}",
            requires_key=False,
            priority=15,
            max_concurrency=6,
            input_cost_per_mtok=0.0,
            output_cost_per_mtok=0.0,
            cost_basis=f"listed $0 in/out — {_ZEN_SOURCE}",
            roles=[Role.EVALUATOR, Role.PARALLEL_WORKER, Role.EXPLORE, Role.RESEARCH],
            notes=(
                "The cheapest useful free route, and the reason is measurable "
                "rather than a guess: 1.6s, ZERO reasoning tokens, and a prompt "
                "cache hit, where every other free Zen route spends most of a "
                "small budget thinking before answering. Preferred for judging "
                "and high-volume clerical work, where hidden reasoning buys "
                "latency and truncation risk and nothing else."
            ),
        ),
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
            free_note=f"Free on Zen, serving keyless 2026-08-26. {_ZEN_SOURCE}",
            requires_key=False,
            priority=20,
            max_concurrency=4,
            input_cost_per_mtok=0.0,
            output_cost_per_mtok=0.0,
            cost_basis=f"listed $0 in/out — {_ZEN_SOURCE}",
            roles=[Role.MUTATION, Role.RESEARCH, Role.EVALUATOR, Role.PARALLEL_WORKER],
            notes="Verified 2026-08-26: HTTP 200 keyless in 2.1s, 43 reasoning tokens.",
        ),
        ModelProfile(
            id="zen-deepseek-v4-flash",
            provider="opencode_zen",
            model="deepseek-v4-flash",
            api_base=OPENCODE_ZEN_BASE,
            secret_ref="OPENCODE_API_KEY",
            display_name="DeepSeek v4 Flash (OpenCode Zen)",
            declared_capabilities=[
                Capability.CHAT, Capability.TOOLS, Capability.STREAMING,
            ],
            free_status=FreeStatus.PAID,
            free_note=(
                "NOT free and NOT keyless. Re-probed 2026-08-26: returns "
                "`AuthError: Missing API key` without a credential. The free "
                "variant is a different id, `deepseek-v4-flash-free`, which is "
                "listed but returns HTTP 400 'Model is unavailable'."
            ),
            # Corrected 2026-08-26. This was `requires_key=False`, inherited
            # from Ox Alpha's genuinely keyless behaviour; the flag put an
            # unusable route into chains as though it were free.
            requires_key=True,
            priority=40,
            max_concurrency=6,
            roles=[Role.EXPLORE, Role.RESEARCH, Role.PARALLEL_WORKER],
            notes="Fast route for low-value parallel work; also tools-capable. "
                  "Needs OPENCODE_API_KEY — paid.",
        ),
        # ---------------- STRONG FALLBACK: NVIDIA NIM ----------------
        # Every id below was read out of NVIDIA's live catalogue on
        # 2026-08-26 (`GET /v1/models`, no credential needed). Inference is
        # UNVERIFIED — no NVIDIA_API_KEY exists in this repo — so capabilities
        # are declared, never claimed as measured, and the doctor overrules
        # them the moment a key is present.
        ModelProfile(
            id="nim-nemotron-3-ultra",
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
            priority=50,
            max_concurrency=4,
            roles=[
                Role.EMERGENCY, Role.MUTATION, Role.DEEP_CODING, Role.ORCHESTRATOR,
                Role.PLANNING, Role.REVIEW, Role.ARCHITECTURE,
            ],
            notes=(
                "VERIFIED 2026-08-28 with a real key: 4.5 s with tools. The "
                "flagship reasoner, and the primary route for mutation work. "
                "Replaces `deepseek-ai/deepseek-v4-pro`, which this project once "
                "configured as its strong fallback and which is NOT in NVIDIA's "
                "catalogue at all — that fallback could never have served."
            ),
        ),
        # The fastest working route measured on any provider here, free or paid.
        ModelProfile(
            id="nim-nemotron-3-super-120b",
            provider="nvidia_nim",
            model="nvidia/nemotron-3-super-120b-a12b",
            api_base=NVIDIA_NIM_BASE,
            secret_ref="NVIDIA_API_KEY",
            display_name="Nemotron 3 Super 120B (NVIDIA NIM)",
            declared_capabilities=[
                Capability.CHAT, Capability.TOOLS, Capability.STREAMING,
            ],
            free_status=FreeStatus.UNKNOWN,
            free_note="NIM credit terms depend on the account; probe at runtime.",
            priority=10,
            max_concurrency=4,
            roles=[
                Role.EVALUATOR, Role.RESEARCH, Role.PARALLEL_WORKER, Role.EXPLORE,
                Role.ORCHESTRATOR, Role.PLANNING, Role.REVIEW, Role.ARCHITECTURE,
                Role.MUTATION, Role.EMERGENCY,
            ],
            notes=(
                "VERIFIED 2026-08-28: 732 ms with tools — the fastest working "
                "route measured on any provider here. Carries the latency-bound "
                "roles for that reason."
            ),
        ),
        ModelProfile(
            id="nim-nemotron-3-nano-30b",
            provider="nvidia_nim",
            model="nvidia/nemotron-3-nano-30b-a3b",
            api_base=NVIDIA_NIM_BASE,
            secret_ref="NVIDIA_API_KEY",
            display_name="Nemotron 3 Nano 30B (NVIDIA NIM)",
            declared_capabilities=[Capability.CHAT, Capability.STREAMING],
            free_status=FreeStatus.UNKNOWN,
            free_note="NIM credit terms depend on the account; probe at runtime.",
            priority=20,
            max_concurrency=4,
            roles=[
                Role.EVALUATOR, Role.RESEARCH, Role.PARALLEL_WORKER, Role.EXPLORE,
            ],
            notes=(
                "VERIFIED 2026-08-28: serves. Note the spelling — "
                "`nvidia/nemotron-nano-3-30b-a3b` is ALSO in NVIDIA's catalogue "
                "and returns 404 'Model not found'. Two transposed words, only "
                "one real; this project had the broken one configured. Do not "
                "retype this id from memory."
            ),
        ),
        ModelProfile(
            id="nim-deepseek-v4-flash",
            provider="nvidia_nim",
            model="deepseek-ai/deepseek-v4-flash-0731",
            api_base=NVIDIA_NIM_BASE,
            secret_ref="NVIDIA_API_KEY",
            display_name="DeepSeek V4 Flash (NVIDIA NIM)",
            declared_capabilities=[Capability.CHAT, Capability.STREAMING],
            free_status=FreeStatus.UNKNOWN,
            free_note="NIM credit terms depend on the account; probe at runtime.",
            priority=60,
            max_concurrency=2,
            roles=[Role.DEEP_CODING, Role.MUTATION],
            notes=(
                "VERIFIED 2026-08-28: serves, but at 51 s — strong and slow "
                "enough to matter. Ranked below the generalists deliberately."
            ),
        ),
        ModelProfile(
            id="nim-gpt-oss-120b",
            enabled=False,
            provider="nvidia_nim",
            model="openai/gpt-oss-120b",
            api_base=NVIDIA_NIM_BASE,
            secret_ref="NVIDIA_API_KEY",
            display_name="GPT-OSS 120B (NVIDIA NIM)",
            declared_capabilities=[
                Capability.CHAT, Capability.TOOLS, Capability.STREAMING,
            ],
            free_status=FreeStatus.UNKNOWN,
            priority=55,
            max_concurrency=4,
            roles=[Role.MUTATION, Role.PLANNING, Role.ARCHITECTURE, Role.REVIEW],
            notes="In catalogue 2026-08-26; inference UNVERIFIED (no key).",
        ),
        ModelProfile(
            id="nim-kimi-k3",
            provider="nvidia_nim",
            model="moonshotai/kimi-k3",
            api_base=NVIDIA_NIM_BASE,
            secret_ref="NVIDIA_API_KEY",
            display_name="Kimi K3 (NVIDIA NIM)",
            declared_capabilities=[
                Capability.CHAT, Capability.TOOLS, Capability.STREAMING,
            ],
            free_status=FreeStatus.UNKNOWN,
            priority=60,
            max_concurrency=4,
            roles=[Role.DEEP_CODING, Role.ORCHESTRATOR, Role.EXPLORE],
            notes=(
                "Replaces `qwen/qwen2.5-coder-32b-instruct`, which is not in "
                "NVIDIA's catalogue — NIM hosts no Qwen model at all as of "
                "2026-08-26. In catalogue; inference UNVERIFIED (no key)."
            ),
        ),
        ModelProfile(
            id="nim-codestral-22b",
            enabled=False,
            provider="nvidia_nim",
            model="mistralai/codestral-22b-instruct-v0.1",
            api_base=NVIDIA_NIM_BASE,
            secret_ref="NVIDIA_API_KEY",
            display_name="Codestral 22B (NVIDIA NIM)",
            declared_capabilities=[Capability.CHAT, Capability.STREAMING],
            free_status=FreeStatus.UNKNOWN,
            priority=65,
            max_concurrency=4,
            roles=[Role.MUTATION, Role.PARALLEL_WORKER, Role.EVALUATOR],
            notes="Code-specialised. In catalogue 2026-08-26; inference UNVERIFIED.",
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
            priority=90,
            max_concurrency=2,
            roles=[Role.EMERGENCY, Role.PARALLEL_WORKER],
            enabled=False,   # opt-in: only useful if such a server is running
            notes="Enable in settings when a local server (Ollama, vLLM, LM Studio) is up.",
        ),
    ]

def default_role_chains() -> Dict[Role, List[str]]:
    """
    Ordered fallback chain per role.

    Ox Alpha led every chain here until it was withdrawn (see the module
    docstring). `nemotron-3-ultra-free` leads now: it is the strongest route
    measured serving without a credential, so the shipped configuration works
    with no keys at all and improves when keys are added.

    Two orderings are deliberate and measured rather than inherited:

      * **Evaluator and parallel work lead with laguna**, not with the primary.
        It answered in 1.6s with zero reasoning tokens where the primary spent
        39 on the same two-word prompt. Ranking and clerical work do not need
        hidden reasoning, and paying for it buys latency and truncation risk
        and nothing else.
      * **Key-gated NIM routes sit behind keyless Zen routes** in every chain.
        NIM is plausibly stronger and is entirely unverified here; a route that
        cannot serve without a credential the operator may not have must not
        lead a chain.

    Worth being precise about what is and is not automatic, because it is easy
    to overclaim: the *capability filter* self-corrects in both directions
    without code changes — a failed tools probe records `supports_tools=False`
    and the model drops out of tool-requiring roles on its own. The *chain
    order* is a stated preference and does not self-correct; it took a
    deliberate edit when Ox Alpha vanished, and will take another when
    something better appears.
    """
    # Every chain leads with NIM and ends with the keyless Zen routes. The tail
    # is what makes leading with a key-gated provider safe: a route whose
    # credential is absent is filtered out rather than attempted, so an install
    # with no NVIDIA_API_KEY falls straight through to Zen and still serves.
    #
    # `nim-gpt-oss-120b` and `nim-codestral-22b` appear in none of these: probed
    # 2026-08-28, the first hung (0 bytes after 190 s and again after 230 s) and
    # the second returned 404 "Not found for account", which is an entitlement
    # and not something a catalogue can express. Both are disabled above.
    reasoning_first = [
        "nim-nemotron-3-ultra", "nim-nemotron-3-super-120b",
        "zen-nemotron-3-ultra-free", "zen-hy3-free", "zen-laguna-s21-free",
    ]
    # Cheap, high-volume work: 732 ms beats everything else measured here.
    fast_first = [
        "nim-nemotron-3-super-120b", "nim-nemotron-3-nano-30b",
        "zen-laguna-s21-free", "zen-hy3-free", "zen-nemotron-3-ultra-free",
    ]
    # Tool-using roles are reasoning work that happens to call tools, so they
    # lead with the flagship rather than the fast route: one primary for
    # completion and tool roles alike, which is the shape this table had before
    # the move to NIM and the shape the router's tests assert.
    tools_first = [
        "nim-nemotron-3-ultra", "nim-nemotron-3-super-120b", "nim-kimi-k3",
        "zen-nemotron-3-ultra-free", "zen-laguna-s21-free", "zen-hy3-free",
    ]
    from oe_max.providers.local import local_only

    if local_only():
        # Every role gets every local server, in declaration order. There is no
        # basis for ranking them — which one is "better" depends on what the
        # operator loaded into each — and inventing an order would read as a
        # measurement nobody took.
        local_chain = [profile.id for profile in local_profiles()]
        return {role: list(local_chain) for role in Role}

    def ordered(*groups: List[str]) -> List[str]:
        """
        Concatenate preference groups, keeping first occurrence only.

        The chains are built by prepending a role's specialists to a shared
        tail, so an id that is already in the tail would otherwise appear
        twice -- harmless to selection, but it makes the chain misreport its
        own depth and gives a route two chances at a retry budget meant for
        distinct routes.
        """
        seen: set = set()
        out: List[str] = []
        for group in groups:
            for profile_id in group:
                if profile_id not in seen:
                    seen.add(profile_id)
                    out.append(profile_id)
        return out

    return {
        Role.MUTATION: reasoning_first,
        Role.EVALUATOR: fast_first,
        Role.RESEARCH: fast_first,
        Role.PARALLEL_WORKER: fast_first,
        Role.ORCHESTRATOR: tools_first,
        Role.DEEP_CODING: ordered(["nim-kimi-k3", "nim-nemotron-3-ultra",
                                   "nim-deepseek-v4-flash"], tools_first),
        Role.PLANNING: tools_first,
        Role.REVIEW: tools_first,
        Role.ARCHITECTURE: tools_first,
        # EXPLORE is high-volume scanning, not deep reasoning, so it belongs on
        # the fast chain rather than behind the flagship.
        Role.EXPLORE: fast_first,
        Role.EMERGENCY: ["nim-nemotron-3-ultra", "nim-nemotron-3-super-120b",
                         "zen-nemotron-3-ultra-free", "local-openai-compatible"],
    }
