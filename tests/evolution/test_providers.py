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


def test_ox_alpha_is_never_routed_to_tool_requiring_roles(env_keys):
    """
    Ox Alpha currently fails on any request carrying a `tools` array
    (anomalyco/opencode #44300). Routing agent work to it would fail every
    agent run, so capability must override preference.
    """
    r = ModelRouter()
    for role in (Role.ORCHESTRATOR, Role.DEEP_CODING, Role.PLANNING,
                 Role.REVIEW, Role.ARCHITECTURE):
        chosen = r.select(role)
        assert chosen.id != "zen-ox-alpha-free"
        assert chosen.supports(Capability.TOOLS)


def test_exclusion_reason_is_explained_for_the_preferred_model(env_keys):
    r = ModelRouter()
    _, reasons = r.candidates(Role.DEEP_CODING)
    why = reasons.get("zen-ox-alpha-free", "")
    assert "tools" in why  # the operator must be told why their default is not used


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


def test_missing_credential_excludes_a_route(monkeypatch):
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    r = ModelRouter()
    with pytest.raises(NoRouteAvailable) as exc:
        r.select(Role.MUTATION)
    assert "credential" in str(exc.value)


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
