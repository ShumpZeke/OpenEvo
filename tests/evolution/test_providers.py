"""
Routing must respect capability, credentials and health — not just preference.

Rewritten 2026-08-26. The previous version of this file pinned nearly every
assertion to the literal id `zen-ox-alpha-free`, and passed for weeks after that
model was withdrawn from OpenCode Zen, because the profile it asserted against
was still in our own table. A test suite that only checks our defaults are
internally consistent cannot notice that the defaults have gone stale.

So the assertions below are about *properties* wherever a property will do:
the primary completion route is whatever leads the chain, tool roles are served
by something that has tools, exclusions carry reasons. Where a specific id is
unavoidable it is derived from `default_role_chains()` rather than typed in.
`test_catalog.py` and `test_doctor_probes.py` cover the parts that catch
staleness against the provider itself.
"""
import pytest
from control_plane.providers.profiles import (
    Capability, FreeStatus, Role, default_profiles, default_role_chains,
)
from control_plane.providers.router import ModelRouter, NoRouteAvailable


@pytest.fixture
def env_keys(monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "test-opencode-key")
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key-value")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")


def _head(role: Role) -> str:
    """The id the chain names first — the stated preference for that role."""
    return default_role_chains()[role][0]


def _profile(pid: str):
    return next(p for p in default_profiles() if p.id == pid)


# --------------------------------------------------------------------------
# The chain leads with something that actually works
# --------------------------------------------------------------------------

def test_the_preferred_completion_route_is_the_one_selected(env_keys):
    r = ModelRouter()
    assert r.select(Role.MUTATION).id == _head(Role.MUTATION)


def test_every_chain_leads_with_an_enabled_usable_route(env_keys):
    """
    The failure this catches is the one that actually happened: every chain led
    with a withdrawn model, so the shipped default configuration routed every
    role to something that could not serve.
    """
    profiles = {p.id: p for p in default_profiles()}
    for role, chain in default_role_chains().items():
        assert chain, f"{role.value} has an empty chain"
        lead = profiles[chain[0]]
        assert lead.enabled, f"{role.value} leads with disabled {lead.id}"
        assert lead.usable(), f"{role.value} leads with unusable {lead.id}"


def test_tool_roles_lead_with_a_route_that_declares_tools():
    profiles = {p.id: p for p in default_profiles()}
    for role in (Role.ORCHESTRATOR, Role.DEEP_CODING, Role.PLANNING,
                 Role.REVIEW, Role.ARCHITECTURE, Role.EXPLORE):
        for pid in default_role_chains()[role]:
            assert Capability.TOOLS in profiles[pid].declared_capabilities, (
                f"{role.value} chains through {pid}, which does not declare tools"
            )


def test_a_withdrawn_route_never_appears_in_any_chain():
    """
    A withdrawn model is kept as a disabled profile so the Models page can
    explain what happened to it. Keeping it must not put it back in a chain.

    Note the distinction from an *opt-in* disabled route: the local endpoint is
    disabled by default and legitimately sits at the end of the emergency chain,
    ready for an operator who turns it on. A withdrawn route can never be turned
    on, so it carries no roles, and that is what is checked here.
    """
    profiles = {p.id: p for p in default_profiles()}
    withdrawn = {p.id for p in profiles.values() if not p.enabled and not p.roles}
    assert withdrawn, "expected at least one withdrawn route in the table"
    for role, chain in default_role_chains().items():
        for pid in chain:
            assert pid not in withdrawn, f"{role.value} chains through withdrawn {pid}"


def test_an_opt_in_disabled_route_is_last_in_any_chain_it_joins():
    """A route that is off by default must never be preferred over a live one."""
    profiles = {p.id: p for p in default_profiles()}
    for role, chain in default_role_chains().items():
        for i, pid in enumerate(chain):
            if not profiles[pid].enabled:
                assert i == len(chain) - 1, (
                    f"{role.value} chains through disabled {pid} at position {i}, "
                    f"ahead of {chain[i + 1:]}"
                )


def test_tool_roles_are_served_by_a_tools_capable_model(env_keys):
    r = ModelRouter()
    for role in (Role.ORCHESTRATOR, Role.DEEP_CODING, Role.PLANNING,
                 Role.REVIEW, Role.ARCHITECTURE):
        chosen = r.select(role)
        assert chosen.supports(Capability.TOOLS), f"{role.value} -> {chosen.id}"
        # select() reserves a concurrency slot; release it, or a later role
        # sheds to the fallback purely because of in-flight accounting.
        r.release(chosen.id, ok=True, latency_ms=10.0)


# --------------------------------------------------------------------------
# Measured capability wins over stated preference, in both directions
# --------------------------------------------------------------------------

def test_a_failed_tools_probe_removes_the_primary_from_tool_roles(env_keys):
    """
    The invariant to keep permanently: routing follows *measured* capability. If
    the leading tools route regresses, the next probe records supports_tools =
    False and the chain falls through on its own — no code change, no stale
    hardcoded exclusion.

    This is the test to keep even when every model name in this file is obsolete
    again.
    """
    r = ModelRouter()
    lead = _head(Role.DEEP_CODING)
    r.profiles[lead].verified_capabilities = [Capability.CHAT]   # as a failed probe sets

    chosen = r.select(Role.DEEP_CODING)
    assert chosen.id != lead
    assert chosen.supports(Capability.TOOLS)

    _, reasons = r.candidates(Role.DEEP_CODING)
    why = reasons.get(lead, "")
    assert "tools" in why and "verified" in why, why


def test_a_chat_only_route_still_serves_completion_roles(env_keys):
    """A tools regression must not cost the route its completion work."""
    r = ModelRouter()
    lead = _head(Role.MUTATION)
    r.profiles[lead].verified_capabilities = [Capability.CHAT]
    assert r.select(Role.MUTATION).id == lead


def test_an_intermittent_tools_route_is_not_declared_tools_capable():
    """
    `zen-laguna-s-2.1-free` emitted a tool call on 1 of 3 attempts on
    2026-08-26, failing the others with "Endpoint is unavailable". It is the
    fastest chat route measured and is used for exactly that, but it must not
    declare a capability it provides a third of the time — an agent role would
    inherit a one-in-three failure it cannot retry its way out of.
    """
    laguna = _profile("zen-laguna-s-2.1-free")
    assert Capability.CHAT in laguna.declared_capabilities
    assert Capability.TOOLS not in laguna.declared_capabilities
    assert "1 of 3" in laguna.notes or "1/3" in laguna.notes


def test_exclusion_reasons_are_explained_for_every_excluded_model(env_keys):
    """
    The operator must see why any model is not serving a role, and a capability
    exclusion must say whether it was *measured* or merely assumed — "X lacks
    tools" means something very different in each case.
    """
    r = ModelRouter()
    lead = _head(Role.DEEP_CODING)
    r.profiles[lead].verified_capabilities = [Capability.CHAT]
    _, reasons = r.candidates(Role.DEEP_CODING)

    assert reasons, "no exclusions explained at all"
    assert all(v and v.strip() for v in reasons.values()), reasons

    for pid, why in reasons.items():
        if "lacks capability" in why:
            assert ("verified by provider doctor" in why
                    or "not yet probed" in why), f"{pid}: {why}"

    assert "verified by provider doctor" in reasons[lead]


# --------------------------------------------------------------------------
# Free status
# --------------------------------------------------------------------------

def test_no_profile_claims_permanent_or_unlimited_free_access():
    """
    Acceptance criterion 27, checked across the whole table rather than against
    one model. The old version of this test only inspected Ox Alpha, so a new
    free route could have claimed anything it liked.

    Note it checks for an *assertion* of unlimited access — an explicit
    disclaimer is the desired text.
    """
    forbidden = ("is unlimited", "always free", "permanently free",
                 "unlimited free", "free forever", "no limits", "forever free")
    for p in default_profiles():
        text = f"{p.free_note} {p.cost_basis} {p.notes}".lower()
        for claim in forbidden:
            assert claim not in text, f"{p.id} claims {claim!r}"


def test_a_free_now_route_is_limited_time_not_free():
    """
    Zen's free replies carry `cost: "0"`, which is evidence they are free *now*
    and none at all that they will stay free. FREE is reserved for something
    like local hardware, where the claim is structural. Ox Alpha is the standing
    counter-example: documented free, then withdrawn.
    """
    for p in default_profiles():
        if p.provider == "opencode_zen" and p.free_status is not FreeStatus.UNKNOWN:
            assert p.free_status in (FreeStatus.FREE_LIMITED_TIME, FreeStatus.PAID), (
                f"{p.id} is {p.free_status.value}"
            )
            if p.free_status is FreeStatus.FREE_LIMITED_TIME:
                assert "permanent" in p.free_note.lower() or "limited" in p.free_note.lower()


def test_unprobed_routes_report_unknown_free_status():
    """A route we could not probe must not read as free."""
    for p in default_profiles():
        if p.provider == "nvidia_nim":
            assert p.free_status is FreeStatus.UNKNOWN, p.id


def test_unverified_routes_say_so_in_their_notes():
    """
    Standing rule: NVIDIA NIM and OpenRouter are unverified in this repo and
    labelled as such everywhere. Both now carry catalogue-checked model ids,
    which is a real improvement and still not a serving verification.
    """
    for p in default_profiles():
        if p.provider in ("nvidia_nim", "openrouter"):
            assert "not verified" in p.notes.lower() or "unverified" in p.notes.lower(), p.id


def test_a_withdrawn_route_is_disabled_and_explains_itself():
    """
    Ox Alpha is gone: absent from Zen's catalogue and answering HTTP 401 "Model
    x-preview-f-free is not supported". It stays in the table, disabled, so the
    operator's preferred route does not simply vanish from the Models page
    without explanation.
    """
    ox = _profile("zen-ox-alpha-free")
    assert ox.enabled is False
    assert ox.roles == []
    assert "withdrawn" in ox.notes.lower()
    assert "2026-08-26" in ox.notes


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------

def test_missing_credential_excludes_a_route_that_needs_one(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    r = ModelRouter()
    _, reasons = r.candidates(Role.EMERGENCY)
    nim = [p.id for p in default_profiles() if p.provider == "nvidia_nim"]
    assert any("NVIDIA_API_KEY" in reasons.get(pid, "") for pid in nim), reasons


def test_keyless_capable_routes_are_not_excluded_without_a_key(monkeypatch):
    """
    OpenCode Zen serves its free tier with no Authorization header — verified
    2026-08-26 across four models. Treating a missing key as disqualifying would
    switch off every working route in the table.
    """
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    r = ModelRouter()
    chosen = r.select(Role.MUTATION)
    assert chosen.requires_key is False
    assert chosen.usable()


def test_a_paid_route_is_not_marked_keyless():
    """
    `deepseek-v4-flash` was configured keyless and returns HTTP 401 "Missing API
    key." It is Zen's paid tier. Marking a paid route keyless puts it in the
    chain, where it fails every call and burns the circuit breaker.
    """
    paid = _profile("zen-deepseek-v4-flash")
    assert paid.requires_key is True
    assert paid.free_status is FreeStatus.PAID


def test_a_local_endpoint_needs_no_credential():
    """
    `requires_key` defaults to True, so a local server was excluded with
    "missing credential None" even after the operator enabled it.
    """
    local = _profile("local-openai-compatible")
    assert local.secret_ref is None
    assert local.requires_key is False
    local.enabled = True
    assert local.usable()


def test_no_route_when_every_candidate_needs_an_absent_key(monkeypatch):
    for var in ("NVIDIA_API_KEY", "OPENROUTER_API_KEY", "OPENCODE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    r = ModelRouter()
    for prof in r.profiles.values():
        if prof.provider == "opencode_zen":
            prof.enabled = False
    with pytest.raises(NoRouteAvailable) as exc:
        r.select(Role.EMERGENCY)
    assert "credential" in str(exc.value) or "disabled" in str(exc.value)


# --------------------------------------------------------------------------
# Health, circuits, concurrency
# --------------------------------------------------------------------------

def test_circuit_opens_after_repeated_failures_and_fails_over(env_keys):
    r = ModelRouter(failure_threshold=3)
    chosen = r.select(Role.MUTATION)
    lead = _head(Role.MUTATION)
    assert chosen.id == lead
    for _ in range(3):
        r.release(chosen.id, ok=False, error="500")
    assert r.health[chosen.id].is_open()
    assert r.select(Role.MUTATION).id != lead


def test_circuit_can_be_reset(env_keys):
    lead = _head(Role.MUTATION)
    r = ModelRouter(failure_threshold=2)
    for _ in range(2):
        r.release(lead, ok=False)
    assert r.health[lead].is_open()
    r.reset_circuit(lead)
    assert not r.health[lead].is_open()


def test_concurrency_limit_sheds_to_the_next_route(env_keys):
    r = ModelRouter()
    lead = _head(Role.MUTATION)
    for _ in range(r.profiles[lead].max_concurrency):
        assert r.select(Role.MUTATION).id == lead
    assert r.select(Role.MUTATION).id != lead


def test_force_pins_a_role_to_one_model(env_keys):
    r = ModelRouter()
    target = next(p.id for p in default_profiles()
                  if p.provider == "nvidia_nim" and p.enabled)
    r.force(Role.MUTATION, target)
    assert r.select(Role.MUTATION).id == target


# --------------------------------------------------------------------------
# Table integrity
# --------------------------------------------------------------------------

def test_every_role_has_a_configured_chain():
    chains = default_role_chains()
    ids = {p.id for p in default_profiles()}
    for role in Role:
        assert role in chains, f"{role} has no fallback chain"
        for pid in chains[role]:
            assert pid in ids, f"{role} chain references unknown profile {pid}"


def test_profile_ids_are_unique():
    ids = [p.id for p in default_profiles()]
    assert len(ids) == len(set(ids)), "duplicate profile ids"


def test_every_profile_carries_a_wire_model_id_and_base():
    for p in default_profiles():
        assert p.model and p.model.strip(), p.id
        assert p.api_base.startswith(("http://", "https://")), p.id


def test_route_table_is_serialisable(env_keys):
    import json
    json.dumps(ModelRouter().snapshot())
