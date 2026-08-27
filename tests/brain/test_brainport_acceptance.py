"""
Acceptance tests for the BrainPort refactor.

These tests encode the REQUIRED acceptance criteria from the spec.
They must pass after the refactor, and many will fail on the old architecture
until the provider strings are removed from the core.

Run: pytest tests/test_brainport_acceptance.py -v
"""

import os
import re
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]

# Strings that must NOT appear in the evolutionary core (brain + openevolve + oe_max/evaluation etc.)
# Legacy paths that ARE allowed to contain them (temporary adapter):
LEGACY_ALLOWED = {
    "oe_max/providers",
    "oe_max/router.py",
    "oe_max/limiter.py",
    "oe_max/broker",
    "control_plane/providers",
    "tests/",  # tests may mention them as negative examples
    ".git/",
    "oe_max/brain/legacy_adapter.py",  # the one adapter that wraps legacy
}

FORBIDDEN_PATTERNS = [
    "x-preview-f-free",
    "nemotron-3-ultra-free",
    "nemotron-3.5-lightning-free",
    "laguna-s-2.1-free",
    "hy3-free",
    "stealth/ox-alpha",
    "integrate.api.nvidia.com",
    "opencode.ai/zen/v1",
    "openrouter.ai/api/v1",
]

PROVIDER_ENV_VARS = ["NVIDIA_API_KEY", "OPENCODE_ZEN_API_KEY", "OPENROUTER_API_KEY"]

# The RPC surface the OpenCode plugin drives the worker through. Kept next to
# the plugin's tool list because the two are one contract: a tool with no RPC
# behind it fails at runtime, not at build.
WORKER_RPCS = [
    "evolve/start", "evolve/status", "evolve/inspect", "evolve/candidates",
    "evolve/apply", "evolve/pause", "evolve/resume", "evolve/stop",
    "brain/health", "brain/generate",
]

PLUGIN_TOOLS = [
    "evolve_start", "evolve_status", "evolve_inspect", "evolve_candidates",
    "evolve_apply", "evolve_pause", "evolve_resume", "evolve_stop",
]


def _is_legacy(path: pathlib.Path) -> bool:
    rel = str(path.relative_to(REPO)).replace("\\", "/")
    return any(rel.startswith(p) for p in LEGACY_ALLOWED)


def _core_py_files():
    """Core = oe_max/brain, openevolve, oe_max/evaluation, execution, search, archives, etc., excluding legacy."""
    for p in REPO.rglob("*.py"):
        if _is_legacy(p):
            continue
        # Core includes brain (except legacy_adapter), openevolve, and oe_max evolution pieces
        rel = str(p.relative_to(REPO)).replace("\\", "/")
        if rel.startswith("oe_max/brain/") and "legacy_adapter" not in rel:
            yield p
        elif rel.startswith("openevolve/"):
            yield p
        elif rel.startswith("oe_max/evaluation/"):
            yield p
        elif rel.startswith("oe_max/execution/"):
            yield p
        elif rel.startswith("oe_max/search/"):
            yield p
        elif rel.startswith("oe_max/archives.py"):
            yield p


def test_brain_core_contains_no_hardcoded_provider_strings():
    failures = []
    for pat in FORBIDDEN_PATTERNS:
        for f in _core_py_files():
            text = f.read_text(encoding="utf-8", errors="ignore")
            if pat in text:
                failures.append(f"{f.relative_to(REPO)} contains forbidden '{pat}'")
    assert not failures, "Forbidden provider strings in core:\n" + "\n".join(failures)


def test_brain_core_contains_no_provider_env_vars():
    failures = []
    for var in PROVIDER_ENV_VARS:
        for f in _core_py_files():
            text = f.read_text(encoding="utf-8", errors="ignore")
            if var in text:
                failures.append(f"{f.relative_to(REPO)} contains provider env var '{var}'")
    assert not failures, "Provider env vars in core:\n" + "\n".join(failures)


# A role->provider matrix is the thing being designed out: it is what makes a
# vanished model a source change. `ProviderRole` is the concrete symbol the
# legacy stack used for it, so its presence in core is the detectable form.
ROLE_MATRIX_SYMBOLS = ("ProviderRole", "ROLE_CHAINS", "role_chain")


def test_brain_core_contains_no_hardcoded_role_to_provider_matrix():
    """No file in core may carry a role->provider mapping."""
    failures = []
    for f in _core_py_files():
        text = f.read_text(encoding="utf-8", errors="ignore")
        for symbol in ROLE_MATRIX_SYMBOLS:
            if symbol in text:
                failures.append(f"{f.relative_to(REPO)} contains {symbol!r}")
    assert not failures, (
        "role->provider matrix in core (it belongs in legacy): "
        + "; ".join(failures))


def test_brainport_interface_exists():
    from oe_max.brain import BrainPort, BrainRequest, BrainResponse, BrainCapabilities
    from oe_max.brain.capabilities import Capability
    from oe_max.brain.types import PolicyMode, Operation

    # Verify abstract interface
    assert hasattr(BrainPort, "generate")
    assert hasattr(BrainPort, "capabilities")
    assert hasattr(BrainPort, "health_check")

    # Request describes work, not vendor
    req = BrainRequest(objective="optimize foo", policy=PolicyMode.MUTATION_GENERATION)
    d = req.to_dict()
    assert "objective" in d
    assert "provider" not in d
    assert "model" not in d or d.get("model") is None  # must not require vendor model id


def test_policy_modes_replace_roles():
    from oe_max.brain.policies import POLICY_INSTRUCTIONS, instruction_for
    from oe_max.brain.types import PolicyMode

    # Every PolicyMode should have an instruction (no provider)
    for mode in PolicyMode:
        instr = instruction_for(mode)
        assert isinstance(instr, str) and len(instr) > 10
        # Instruction must not contain provider URLs or model IDs
        for pat in FORBIDDEN_PATTERNS:
            assert pat not in instr, f"policy {mode} leaked provider string {pat}"


def test_capability_negotiation_not_model_name():
    from oe_max.brain.capabilities import BrainCapabilities, Capability

    caps = BrainCapabilities(text=True, structured_output=True, context_limit=100000)
    assert caps.has(Capability.TEXT)
    assert caps.has(Capability.STRUCTURED_OUTPUT)
    assert not caps.has(Capability.VISION)
    # No model name check
    caps2 = BrainCapabilities.from_dict(caps.to_dict())
    assert caps2.context_limit == 100000


def test_legacy_adapter_is_isolated():
    """Legacy adapter is the ONLY file allowed to import old providers, and core must not import them at top level."""
    for f in _core_py_files():
        if f.name == "legacy_adapter.py":
            continue
        text = f.read_text(encoding="utf-8", errors="ignore")
        # Forbid direct top-level imports of old provider stack; lazy imports inside a function are tolerated for worker entrypoints
        assert "from oe_max.providers" not in text, f"{f.relative_to(REPO)} imports oe_max.providers (use BrainPort)"
        assert "import oe_max.providers" not in text, f"{f.relative_to(REPO)} imports oe_max.providers"
        assert "from oe_max.router" not in text, f"{f.relative_to(REPO)} imports oe_max.router"
        assert "from oe_max.limiter" not in text, f"{f.relative_to(REPO)} imports oe_max.limiter"
        assert "from control_plane.providers" not in text, f"{f.relative_to(REPO)} imports control_plane.providers"


def test_brain_request_never_requires_vendor_model_id():
    from oe_max.brain.types import BrainRequest

    req = BrainRequest(objective="test")
    # Must be constructible without any model/provider field
    assert req.objective == "test"
    # Serialization round-trip must not introduce vendor fields
    d = req.to_dict()
    assert "model" not in d or d.get("model") is None
    assert "provider" not in d


def test_worker_declares_every_rpc_the_plugin_calls():
    import pathlib

    worker = REPO / "oe_max" / "brain" / "worker.py"
    assert worker.exists(), "worker.py missing"
    text = worker.read_text(encoding="utf-8")
    for method in WORKER_RPCS:
        assert method in text, f"worker missing RPC {method}"


def test_worker_answers_the_hello_handshake():
    """
    Actually spawn the worker and read its first line.

    This used to be wrapped in a bare `except Exception: pass` "to tolerate
    Windows pipe quirks", which meant every assertion in it was discarded and
    the gate could not fail -- a worker that never started still reported a
    pass. Closing stdin via `communicate(input=b"")` is what avoids the pipe
    deadlock, so the check can simply be real instead.
    """
    import json
    import subprocess
    import sys

    proc = subprocess.Popen(
        [sys.executable, "-m", "oe_max.brain.worker", "--brain", "null"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = proc.communicate(input=b"", timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise AssertionError(
            "worker did not answer the hello handshake within 30s")

    stderr = err.decode("utf-8", "replace").strip()
    assert out.strip(), f"worker produced no output; stderr was: {stderr[-500:]}"

    msg = json.loads(out.splitlines()[0].decode("utf-8"))
    assert msg.get("type") == "hello", f"first line was not a hello: {msg!r}"
    assert msg.get("worker_version"), "hello carries no worker_version"
    # The handshake is what tells the host what it may ask for; a hello that
    # announces no capabilities would let the plugin request work the brain
    # cannot do.
    assert isinstance(msg.get("capabilities"), dict), "hello carries no capabilities"


def test_plugin_exposes_every_evolution_tool():
    """
    Read the plugin *source*, not `dist/`.

    `dist/` is TypeScript build output and is not committed, so asserting on it
    passes only on a machine that happens to have run the build and fails on a
    fresh clone -- which says nothing about the plugin.
    """
    plugin_ts = REPO / "packages" / "opencode-plugin" / "src" / "index.ts"
    assert plugin_ts.exists(), "plugin source (packages/opencode-plugin/src/index.ts) missing"
    text = plugin_ts.read_text(encoding="utf-8", errors="ignore")
    for tool in PLUGIN_TOOLS:
        assert tool in text, f"plugin missing tool {tool}"
    # The plugin must default to the host's selected model, never name one.
    assert "inherit" in text
    for pat in FORBIDDEN_PATTERNS:
        assert pat not in text, f"plugin source hardcodes provider string {pat}"
