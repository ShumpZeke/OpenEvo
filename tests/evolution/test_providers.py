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


PRIMARY = "zen-nemotron-3-ultra-free"


def test_the_primary_is_the_preferred_completion_route(env_keys):
    r = ModelRouter()
    assert r.select(Role.MUTATION).id == PRIMARY


def test_the_withdrawn_ox_alpha_route_can_never_be_selected(env_keys):
    """
    Ox Alpha was this project's stated primary and no longer exists. Probed
    2026-08-26: absent from Zen's /models, and a completion returns
    `ModelError: Model x-preview-f-free is not supported` — removal, not
    gating, since a paid Zen model answers `AuthError: Missing API key`.

    The profile is kept, disabled, so the UI can explain what happened. This
    test is the guard against it being quietly re-promoted from memory: if it
    genuinely returns, re-enabling it is a deliberate edit that must also
    delete this test, which is the point.
    """
    r = ModelRouter()
    assert r.profiles["zen-ox-alpha-free"].enabled is False
    for role in Role:
        assert r.select(role).id != "zen-ox-alpha-free", role
        r.release(r.select(role).id, ok=True, latency_ms=1.0)


def test_the_primary_serves_tool_roles_too(env_keys):
    """The preference applies uniformly: one primary, all roles it can do."""
    r = ModelRouter()
    for role in (Role.ORCHESTRATOR, Role.PLANNING, Role.REVIEW, Role.ARCHITECTURE):
        chosen = r.select(role)
        assert chosen.id == PRIMARY, role
        # select() reserves a concurrency slot; release it, or a later role
        # sheds to the fallback purely because of in-flight accounting.
        r.release(chosen.id, ok=True, latency_ms=10.0)


def test_a_failed_tools_probe_removes_the_primary_from_tool_roles(env_keys):
    """
    The invariant that must hold permanently: routing follows *measured*
    capability, in both directions. If the primary's tool support breaks, the
    next probe records supports_tools=False and the chain falls through on its
    own — no code change, no stale hardcoded exclusion.

    This is the test to keep even when the others become obsolete. It has
    already outlived one primary: it was written against Ox Alpha, and the
    behaviour it pins was the reason replacing that route was a table edit.
    """
    r = ModelRouter()
    primary = r.profiles[PRIMARY]
    primary.verified_capabilities = [Capability.CHAT]   # as a failed probe would set

    chosen = r.select(Role.DEEP_CODING)
    assert chosen.id != PRIMARY
    assert chosen.supports(Capability.TOOLS)

    _, reasons = r.candidates(Role.DEEP_CODING)
    why = reasons.get(PRIMARY, "")
    assert "tools" in why and "verified" in why, why

    # ...and it still leads plain-completion roles, which never needed tools.
    assert r.select(Role.MUTATION).id == PRIMARY


def test_exclusion_reasons_are_explained_for_every_excluded_model(env_keys):
    """
    The operator must be able to see why any model is not serving a role, and a
    capability exclusion must say whether it was *measured* or merely assumed —
    "Ox Alpha lacks tools" means something very different in each case.
    """
    r = ModelRouter()
    r.profiles[PRIMARY].verified_capabilities = [Capability.CHAT]
    _, reasons = r.candidates(Role.DEEP_CODING)

    assert reasons, "no exclusions explained at all"
    assert all(v and v.strip() for v in reasons.values()), reasons

    for pid, why in reasons.items():
        if "lacks capability" in why:
            assert ("verified by provider doctor" in why
                    or "not yet probed" in why), f"{pid}: {why}"

    # The one we deliberately downgraded is reported as measured, not assumed.
    assert "verified by provider doctor" in reasons[PRIMARY]


def test_free_status_is_never_claimed_permanent():
    """
    Acceptance criterion 27: the system must not present a free route as
    permanently or unlimitedly free. Note this checks for an *assertion* of
    unlimited access — an explicit disclaimer is the desired text.

    Ox Alpha's disappearance is the argument for the criterion, not a reason to
    drop it: a route documented "free for a limited time" stopped existing
    inside that limited time. Every free route in the table is checked, so a
    newly-added one cannot skip the rule.
    """
    free = [p for p in default_profiles()
            if p.free_status is FreeStatus.FREE_LIMITED_TIME]
    assert free, "no free-tier routes in the table at all"
    for prof in free:
        note = prof.free_note.lower()
        assert note.strip(), f"{prof.id} claims a free tier with no note"
        for claim in ("is unlimited", "always free", "permanently free",
                      "unlimited free", "free forever"):
            assert claim not in note, f"{prof.id} free_note claims {claim!r}"


def test_unprobed_models_report_unknown_free_status():
    nim = next(p for p in default_profiles() if p.id == "nim-nemotron-3-ultra")
    assert nim.free_status is FreeStatus.UNKNOWN


def test_missing_credential_excludes_a_route_that_needs_one(monkeypatch):
    """
    A route that genuinely requires a key is excluded without it — and the
    exclusion says which environment variable is missing.
    """
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    r = ModelRouter()
    _, reasons = r.candidates(Role.EMERGENCY)
    assert "NVIDIA_API_KEY" in reasons.get("nim-nemotron-3-ultra", "")


def test_keyless_capable_route_is_not_excluded_without_a_key(monkeypatch):
    """
    OpenCode Zen was verified serving `nemotron-3-ultra-free` with no
    Authorization header (HTTP 200, 3.3s, 2026-08-26). Treating a missing key
    as disqualifying would switch off a working primary route — which is
    exactly what the old behaviour did.
    """
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    r = ModelRouter()
    chosen = r.select(Role.MUTATION)
    assert chosen.id == PRIMARY
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
    assert chosen.id == PRIMARY
    for _ in range(3):
        r.release(chosen.id, ok=False, error="500")
    assert r.health[chosen.id].is_open()
    assert r.select(Role.MUTATION).id != PRIMARY


def test_circuit_can_be_reset(env_keys):
    r = ModelRouter(failure_threshold=2)
    for _ in range(2):
        r.release(PRIMARY, ok=False)
    assert r.health[PRIMARY].is_open()
    r.reset_circuit(PRIMARY)
    assert not r.health[PRIMARY].is_open()


def test_concurrency_limit_sheds_to_the_next_route(env_keys):
    r = ModelRouter()
    primary = r.profiles[PRIMARY]
    for _ in range(primary.max_concurrency):
        assert r.select(Role.MUTATION).id == PRIMARY
    assert r.select(Role.MUTATION).id != PRIMARY


def test_force_pins_a_role_to_one_model(env_keys):
    r = ModelRouter()
    r.force(Role.MUTATION, "nim-nemotron-3-ultra")
    assert r.select(Role.MUTATION).id == "nim-nemotron-3-ultra"


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
