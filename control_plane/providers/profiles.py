"""
Provider and model profiles.

Default policy (SOURCE_OF_TRUTH section 16.1): OpenCode Zen / Ox Alpha Free is
the PREFERRED primary route while it is healthy and currently free; NVIDIA NIM
is the strong fallback.

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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "model": self.model,
            "api_base": self.api_base,
            "display_name": self.display_name or self.model,
            "secret_ref": self.secret_ref,
            "secret_present": self.has_secret(),
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
    "Tool-calling reported broken for this model: any request containing a "
    "`tools` array returns 'Upstream request failed: Endpoint is unavailable' "
    "(anomalyco/opencode issue #44300, open 2026-08-25). Plain chat completions "
    "are unaffected. The doctor probes this at runtime; tool-requiring roles "
    "fall back automatically."
)


def default_profiles() -> List[ModelProfile]:
    """
    The shipped default routing table.

    Ox Alpha is priority 0 for completion work — the operator explicitly wants
    the strongest free route used first — while tool-requiring roles are routed
    to verified tools-capable models because of the issue documented above.
    """
    return [
        # ---------------- PREFERRED PRIMARY ----------------
        ModelProfile(
            id="zen-ox-alpha-free",
            provider="opencode_zen",
            model="x-preview-f-free",
            api_base=OPENCODE_ZEN_BASE,
            secret_ref="OPENCODE_API_KEY",
            display_name="Ox Alpha Free (OpenCode Zen)",
            context_limit=1_048_576,
            max_output_tokens=131_072,
            # TOOLS deliberately NOT declared: see _TOOLS_ISSUE.
            declared_capabilities=[
                Capability.CHAT, Capability.JSON_MODE, Capability.STREAMING,
            ],
            free_status=FreeStatus.FREE_LIMITED_TIME,
            free_note=(
                "Documented as free for a limited time; usage limits may change. "
                f"Source: {_ZEN_SOURCE}. Not guaranteed permanent or unlimited."
            ),
            priority=0,
            max_concurrency=4,
            input_cost_per_mtok=0.0,
            output_cost_per_mtok=0.0,
            cost_basis=f"listed $0 in/out/cached — {_ZEN_SOURCE}",
            roles=[
                Role.MUTATION, Role.RESEARCH, Role.EVALUATOR, Role.PARALLEL_WORKER,
            ],
            notes=_TOOLS_ISSUE,
        ),
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
            free_status=FreeStatus.UNKNOWN,
            free_note=f"Listed on Zen; free status must be probed. {_ZEN_SOURCE}",
            priority=10,
            max_concurrency=3,
            roles=[
                Role.ORCHESTRATOR, Role.DEEP_CODING, Role.PLANNING, Role.REVIEW,
                Role.ARCHITECTURE, Role.EXPLORE,
            ],
            notes=(
                "Reported to handle tools correctly on the same Zen route where "
                "Ox Alpha fails; primary agent-harness model until Ox Alpha's "
                "tool support is confirmed working."
            ),
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
            free_status=FreeStatus.UNKNOWN,
            priority=20,
            max_concurrency=6,
            roles=[Role.EXPLORE, Role.RESEARCH, Role.PARALLEL_WORKER],
            notes="Fast route for low-value parallel work; also tools-capable.",
        ),
        # ---------------- STRONG FALLBACK: NVIDIA NIM ----------------
        ModelProfile(
            id="nim-deepseek-v4-pro",
            provider="nvidia_nim",
            model="deepseek-ai/deepseek-v4-pro",
            api_base=NVIDIA_NIM_BASE,
            secret_ref="NVIDIA_API_KEY",
            display_name="DeepSeek v4 Pro (NVIDIA NIM)",
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
            notes="Primary strong fallback pool per SOURCE_OF_TRUTH section 16.1.",
        ),
        ModelProfile(
            id="nim-qwen25-coder-32b",
            provider="nvidia_nim",
            model="qwen/qwen2.5-coder-32b-instruct",
            api_base=NVIDIA_NIM_BASE,
            secret_ref="NVIDIA_API_KEY",
            display_name="Qwen2.5 Coder 32B (NVIDIA NIM)",
            declared_capabilities=[Capability.CHAT, Capability.STREAMING],
            priority=60,
            max_concurrency=4,
            roles=[Role.MUTATION, Role.PARALLEL_WORKER, Role.EVALUATOR],
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

    Ox Alpha leads every completion-only chain (the operator's stated
    preference). Tool-requiring roles lead with a tools-capable model instead —
    not a downgrade of that preference but the only way the role can function
    while issue #44300 is open. The doctor can promote Ox Alpha into these
    chains automatically the moment a live tools probe succeeds.
    """
    zen_first = ["zen-ox-alpha-free", "nim-deepseek-v4-pro", "nim-qwen25-coder-32b"]
    tools_first = [
        "zen-nemotron-3-ultra-free", "nim-deepseek-v4-pro", "zen-deepseek-v4-flash",
    ]
    return {
        Role.MUTATION: zen_first,
        Role.EVALUATOR: zen_first,
        Role.RESEARCH: ["zen-deepseek-v4-flash", "zen-ox-alpha-free", "nim-qwen25-coder-32b"],
        Role.PARALLEL_WORKER: [
            "zen-deepseek-v4-flash", "zen-ox-alpha-free", "nim-qwen25-coder-32b",
        ],
        Role.ORCHESTRATOR: tools_first,
        Role.DEEP_CODING: tools_first,
        Role.PLANNING: tools_first,
        Role.REVIEW: tools_first,
        Role.ARCHITECTURE: tools_first,
        Role.EXPLORE: ["zen-deepseek-v4-flash"] + tools_first,
        Role.EMERGENCY: ["nim-deepseek-v4-pro", "nim-qwen25-coder-32b", "local-openai-compatible"],
    }
