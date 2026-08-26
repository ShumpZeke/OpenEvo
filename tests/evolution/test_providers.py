"""Routing must respect capability, credentials and health — not just preference."""
import os
import pytest
from control_plane.providers.profiles import (
    Capability, FreeStatus, Role, default_profiles, default_role_chains,
)
from control_plane.providers.router import ModelRouter, NoRouteAvailable


@pytest.fixture
def env_keys(monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "test-opencode-key")
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key-value")


def test_ox_alpha_is_the_preferred_completion_route(env_keys):
    r = ModelRouter()
    assert r.select(Role.MUTATION).id == "zen-ox-alpha-free"


def test_ox_alpha_serves_tool_roles_now_that_tools_are_verified(env_keys):
    """
    Ox Alpha is the operator's stated primary and, since anomalyco/opencode
    #44300 was fixed, verifiably supports tools. It should therefore lead
    tool-requiring roles too — the preference applies uniformly.
    """
    r = ModelRouter()
    for role in (Role.ORCHESTRATOR, Role.DEEP_CODING, Role.PLANNING,
                 Role.REVIEW, Role.ARCHITECTURE):
        chosen = r.select(role)
        assert chosen.id == "zen-ox-alpha-free", role
        # select() reserves a concurrency slot; release it, or the fifth role
        # sheds to the fallback purely because of in-flight accounting.
        r.release(chosen.id, ok=True, latency_ms=10.0)


def test_a_failed_tools_probe_removes_ox_alpha_from_tool_roles(env_keys):
    """
    The invariant that must hold permanently: routing follows *measured*
    capability, in both directions. If #44300 regresses, the next probe records
    supports_tools=False and the chain falls through on its own — no code change,
    no stale hardcoded exclusion.

    This is the test to keep even if the others become obsolete again.
    """
    r = ModelRouter()
    ox = r.profiles["zen-ox-alpha-free"]
    ox.verified_capabilities = [Capability.CHAT]        # as a failed probe would set

    chosen = r.select(Role.DEEP_CODING)
    assert chosen.id != "zen-ox-alpha-free"
    assert chosen.supports(Capability.TOOLS)

    _, reasons = r.candidates(Role.DEEP_CODING)
    why = reasons.get("zen-ox-alpha-free", "")
    assert "tools" in why and "verified" in why, why

    # ...and it still leads plain-completion roles, which never needed tools.
    assert r.select(Role.MUTATION).id == "zen-ox-alpha-free"


def test_exclusion_reasons_are_explained_for_every_excluded_model(env_keys):
    """
    The operator must be able to see why any model is not serving a role, and a
    capability exclusion must say whether it was *measured* or merely assumed —
    "Ox Alpha lacks tools" means something very different in each case.
    """
    r = ModelRouter()
    r.profiles["zen-ox-alpha-free"].verified_capabilities = [Capability.CHAT]
    _, reasons = r.candidates(Role.DEEP_CODING)

    assert reasons, "no exclusions explained at all"
    assert all(v and v.strip() for v in reasons.values()), reasons

    for pid, why in reasons.items():
        if "lacks capability" in why:
            assert ("verified by provider doctor" in why
                    or "not yet probed" in why), f"{pid}: {why}"

    # The one we deliberately downgraded is reported as measured, not assumed.
    assert "verified by provider doctor" in reasons["zen-ox-alpha-free"]


def test_free_status_is_never_claimed_permanent():
    """
    Acceptance criterion 27: the system must not present Ox Alpha as permanently
    or unlimitedly free. Note this checks for an *assertion* of unlimited access
    — an explicit disclaimer ("not guaranteed unlimited") is the desired text.
    """
    ox = next(p for p in default_profiles() if p.id == "zen-ox-alpha-free")
    assert ox.free_status is FreeStatus.FREE_LIMITED_TIME
    note = ox.free_note.lower()
    assert "limited time" in note
    for claim in ("is unlimited", "always free", "permanently free",
                  "unlimited free", "free forever"):
        assert claim not in note, f"free_note claims {claim!r}"


def test_unprobed_models_report_unknown_free_status():
    nim = next(p for p in default_profiles() if p.id == "nim-deepseek-v4-pro")
    assert nim.free_status is FreeStatus.UNKNOWN


def test_missing_credential_excludes_a_route_that_needs_one(monkeypatch):
    """
    A route that genuinely requires a key is excluded without it — and the
    exclusion says which environment variable is missing.
    """
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    r = ModelRouter()
    _, reasons = r.candidates(Role.EMERGENCY)
    assert "NVIDIA_API_KEY" in reasons.get("nim-deepseek-v4-pro", "")


def test_keyless_capable_route_is_not_excluded_without_a_key(monkeypatch):
    """
    OpenCode Zen was verified serving `x-preview-f-free` with no Authorization
    header. Treating a missing key as disqualifying would switch off a working
    primary route — which is exactly what the old behaviour did.
    """
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    r = ModelRouter()
    chosen = r.select(Role.MUTATION)
    assert chosen.id == "zen-ox-alpha-free"
    assert chosen.requires_key is False


def test_no_route_when_every_candidate_needs_an_absent_key(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    r = ModelRouter()
    # Emergency chains only through key-requiring providers.
    for pid, prof in r.profiles.items():
        if prof.provider == "opencode_zen":
            prof.enabled = False
    with pytest.raises(NoRouteAvailable) as exc:
        r.select(Role.EMERGENCY)
    assert "credential" in str(exc.value) or "disabled" in str(exc.value)


def test_circuit_opens_after_repeated_failures_and_fails_over(env_keys):
    r = ModelRouter(failure_threshold=3)
    chosen = r.select(Role.MUTATION)
    assert chosen.id == "zen-ox-alpha-free"
    for _ in range(3):
        r.release(chosen.id, ok=False, error="500")
    assert r.health[chosen.id].is_open()
    assert r.select(Role.MUTATION).id != "zen-ox-alpha-free"


def test_circuit_can_be_reset(env_keys):
    r = ModelRouter(failure_threshold=2)
    for _ in range(2):
        r.release("zen-ox-alpha-free", ok=False)
    assert r.health["zen-ox-alpha-free"].is_open()
    r.reset_circuit("zen-ox-alpha-free")
    assert not r.health["zen-ox-alpha-free"].is_open()


def test_concurrency_limit_sheds_to_the_next_route(env_keys):
    r = ModelRouter()
    ox = r.profiles["zen-ox-alpha-free"]
    for _ in range(ox.max_concurrency):
        assert r.select(Role.MUTATION).id == "zen-ox-alpha-free"
    assert r.select(Role.MUTATION).id != "zen-ox-alpha-free"


def test_force_pins_a_role_to_one_model(env_keys):
    r = ModelRouter()
    r.force(Role.MUTATION, "nim-deepseek-v4-pro")
    assert r.select(Role.MUTATION).id == "nim-deepseek-v4-pro"


def test_every_role_has_a_configured_chain():
    chains = default_role_chains()
    ids = {p.id for p in default_profiles()}
    for role in Role:
        assert role in chains, f"{role} has no fallback chain"
        for pid in chains[role]:
            assert pid in ids, f"{role} chain references unknown profile {pid}"


def test_route_table_is_serialisable(env_keys):
    import json
    json.dumps(ModelRouter().snapshot())
