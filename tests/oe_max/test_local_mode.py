"""
Fully local mode: the model runs on this machine, and nothing leaves it.

Every provider in this repository used to be a remote endpoint behind a
credential. The only nod to running locally was one disabled profile in the
control plane and `scripts/local_provider.py`, which is a deterministic stub
rather than a model. The broker — the thing OpenEvolve actually talks to — had
no local provider at all, so "run this offline" was not a supported
configuration however the routing was set.

Two claims are pinned here, and they are different strengths.

**The weak one, and why it is not enough.** "The cloud routes have no key, so
they are filtered out." That is a filter, and a filter depends on every future
code path remembering to apply it. A chain entry, a catalogue file, a refresh
or a pinned model could each put a request back on the wire.

**The strong one.** Under `OE_MAX_LOCAL_ONLY` the commercial adapters are never
constructed. There is nothing to filter, nothing to forget, and no code path
that can reach an endpoint this project configured — because the object holding
its URL does not exist. These tests assert the strong claim.

Verified end to end on 2026-08-28: a broker started with `OE_MAX_LOCAL_ONLY=1`
discovered a local OpenAI-compatible server, and a 6-iteration evolution ran to
`combined_score 1.4198` with 6 requests served, 0 failed, and only the four
local providers present in the process.
"""

import importlib
import os

import pytest

from oe_max.providers import local as local_mod


@pytest.fixture
def fresh_registry(monkeypatch):
    """
    Build a registry with the current environment.

    `local_only()` is read at construction rather than per request — a
    guarantee that can change mid-run is not a guarantee — so a test that wants
    the other mode has to build a new registry, exactly like a new process.
    """
    def build(**env):
        for key, value in env.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        registry = importlib.import_module("oe_max.providers.registry")
        return registry.build_default_registry()

    return build


CLOUD = {"opencode_zen", "nvidia_nim", "openrouter", "groq", "cerebras",
         "gemini", "mistral", "deepseek", "moonshot", "minimax"}


class TestOfflineGuarantee:
    def test_local_only_constructs_no_commercial_provider(self, fresh_registry):
        providers = fresh_registry(OE_MAX_LOCAL_ONLY="1")

        assert set(providers) == set(local_mod.LOCAL_PROVIDER_NAMES)
        assert not (set(providers) & CLOUD), (
            "a commercial provider was constructed in local-only mode; the "
            "guarantee is absence, not an unusable route")

    def test_no_remote_url_is_reachable_in_local_only(self, fresh_registry):
        """
        The point of absence over filtering: there is no object holding a
        remote URL, so no code path can dial one by accident.
        """
        providers = fresh_registry(OE_MAX_LOCAL_ONLY="1")

        for name, adapter in providers.items():
            assert "127.0.0.1" in adapter.base_url or "localhost" in adapter.base_url, (
                f"{name} points off this machine: {adapter.base_url}")

    def test_local_providers_are_also_present_in_normal_mode(self, fresh_registry):
        """
        Local mode is a restriction, not a separate build. The same adapters
        exist alongside the cloud ones so a mixed setup — local model, cloud
        fallback — is a chain ordering question rather than a rebuild.
        """
        providers = fresh_registry(OE_MAX_LOCAL_ONLY=None)

        for name in local_mod.LOCAL_PROVIDER_NAMES:
            assert name in providers, name
        assert "nvidia_nim" in providers

    @pytest.mark.parametrize("value,expected", [
        ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
        ("0", False), ("false", False), ("", False), ("maybe", False),
    ])
    def test_the_switch_reads_the_usual_spellings(self, value, expected):
        assert local_mod.local_only({local_mod.ENV_LOCAL_ONLY: value}) is expected

    def test_the_switch_is_off_when_unset(self):
        assert local_mod.local_only({}) is False


class TestLocalProviders:
    def test_every_local_server_is_keyless(self, fresh_registry):
        """
        A local server needs no credential, and requiring one would defeat the
        purpose: local mode has to work in a checkout with no keys at all.
        """
        providers = fresh_registry(OE_MAX_LOCAL_ONLY="1")

        for name, adapter in providers.items():
            assert adapter.requires_key is False, name
            assert adapter.api_key_env is None, (
                f"{name} names a credential variable; there is no key here")
            assert adapter.usable(), f"{name} is not usable without a key"

    def test_no_local_model_id_is_written_down(self, fresh_registry):
        """
        Rule 6, and local is where it is least negotiable: what a machine serves
        is whatever its operator pulled. Shipping a guess like "llama3.1" would
        be a remembered id of exactly the kind that has bitten this project
        three times.
        """
        providers = fresh_registry(OE_MAX_LOCAL_ONLY="1")

        for name, adapter in providers.items():
            assert adapter.models == {}, (
                f"{name} ships model ids; they must come from /v1/models")
            assert adapter.prefer_patterns, f"{name} would discover nothing"

    def test_the_listing_is_public_so_discovery_doubles_as_liveness(
            self, fresh_registry):
        """
        A local /v1/models needs no authorization, so it is readable exactly
        when the server is up. That makes "is Ollama running?" answerable by
        the discovery request already being made, with no extra probe.
        """
        providers = fresh_registry(OE_MAX_LOCAL_ONLY="1")

        for name, adapter in providers.items():
            assert adapter.public_listing is True, name

    def test_the_timeout_allows_for_slow_local_generation(self, fresh_registry):
        """
        A 30B model on CPU can spend minutes on one mutation and be working
        correctly. A cloud-sized ceiling would manufacture timeouts and then
        blame the model.
        """
        providers = fresh_registry(OE_MAX_LOCAL_ONLY="1")

        for name, adapter in providers.items():
            assert adapter.timeout_s >= 600, (
                f"{name} would time out a slow local model at "
                f"{adapter.timeout_s}s")

    @pytest.mark.parametrize("name,var,port", [
        ("ollama", "OE_MAX_OLLAMA_BASE", "11434"),
        ("lmstudio", "OE_MAX_LMSTUDIO_BASE", "1234"),
        ("vllm", "OE_MAX_VLLM_BASE", "8000"),
        ("llamacpp", "OE_MAX_LLAMACPP_BASE", "8080"),
    ])
    def test_each_server_has_its_conventional_port_and_an_override(
            self, name, var, port, monkeypatch):
        monkeypatch.delenv(var, raising=False)
        assert port in local_mod.base_url_for(name)

        monkeypatch.setenv(var, "http://gpu-box.lan:9999/v1")
        assert local_mod.base_url_for(name) == "http://gpu-box.lan:9999/v1"

    def test_a_trailing_slash_does_not_produce_a_double_slash(self, monkeypatch):
        monkeypatch.setenv("OE_MAX_OLLAMA_BASE", "http://127.0.0.1:11434/v1/")
        assert local_mod.base_url_for("ollama") == "http://127.0.0.1:11434/v1"

    def test_an_unknown_server_is_an_error_not_a_silent_default(self):
        with pytest.raises(KeyError):
            local_mod.base_url_for("not-a-server")


class TestLocalRoutes:
    def test_routes_come_from_discovered_models(self, fresh_registry):
        from oe_max.providers.base import ModelSpec

        providers = fresh_registry(OE_MAX_LOCAL_ONLY="1")
        assert local_mod.local_routes(providers) == [], (
            "routes existed before anything was discovered")

        providers["ollama"].models = {
            "qwen2_5_coder_7b": ModelSpec(key="qwen2_5_coder_7b",
                                          id="qwen2.5-coder:7b", priority=100),
        }
        assert local_mod.local_routes(providers) == [("ollama", "qwen2_5_coder_7b")]

    def test_a_server_that_is_not_running_contributes_nothing(self, fresh_registry):
        """
        Same shape as a provider whose credential is absent: declared, listed
        nothing, contributes no route. It must not be an error — most people
        run one of these four, not all of them.
        """
        providers = fresh_registry(OE_MAX_LOCAL_ONLY="1")

        assert local_mod.local_routes(providers) == []
        assert len(providers) == 4, "all four stay declared regardless"

    def test_is_local_provider_recognises_exactly_the_local_set(self):
        for name in local_mod.LOCAL_PROVIDER_NAMES:
            assert local_mod.is_local_provider(name)
        for name in ("nvidia_nim", "opencode_zen", "openrouter"):
            assert not local_mod.is_local_provider(name)


class TestTheAgentLayerIsAlsoLocal:
    """
    `OE_MAX_LOCAL_ONLY` has to cover both routing layers, not just one.

    The broker's registry is what *evolution* uses. The control plane's
    `ModelRouter` is what the tool-using **agent** uses, and it is a separate
    table with its own profiles. Guaranteeing only the first left the agent able
    to reach a commercial endpoint while the run beside it could not — the kind
    of half-guarantee that is worse than none, because the switch reads as
    covering everything.
    """

    def test_only_local_profiles_are_constructed(self, monkeypatch):
        monkeypatch.setenv(local_mod.ENV_LOCAL_ONLY, "1")
        from control_plane.providers import profiles as profiles_mod

        profiles = profiles_mod.default_profiles()

        assert profiles, "local-only produced no profile at all"
        for profile in profiles:
            assert profile.id.startswith("local-"), profile.id
            assert "127.0.0.1" in profile.api_base, profile.api_base
            assert profile.requires_key is False, profile.id

    def test_every_agent_role_has_a_local_route(self, monkeypatch):
        """
        Including the tool-requiring ones. Declaring CHAT alone was tried and
        left orchestrator, planning, review and architecture with no route at
        all, so the agent could not run locally under any model.
        """
        monkeypatch.setenv(local_mod.ENV_LOCAL_ONLY, "1")
        from control_plane.providers.profiles import Role
        from control_plane.providers.router import ModelRouter

        router = ModelRouter()
        for role in Role:
            chosen = router.select(role)
            router.release(chosen.id, ok=True, latency_ms=1.0)
            assert chosen.id.startswith("local-"), (role.value, chosen.id)

    def test_no_role_chain_names_a_commercial_profile(self, monkeypatch):
        monkeypatch.setenv(local_mod.ENV_LOCAL_ONLY, "1")
        from control_plane.providers import profiles as profiles_mod

        for role, chain in profiles_mod.default_role_chains().items():
            assert chain, f"{role.value} has no chain"
            for profile_id in chain:
                assert profile_id.startswith("local-"), (role.value, profile_id)

    def test_the_commercial_profiles_return_when_the_switch_is_off(self, monkeypatch):
        """Local mode is a restriction, not a different build."""
        monkeypatch.delenv(local_mod.ENV_LOCAL_ONLY, raising=False)
        from control_plane.providers import profiles as profiles_mod

        ids = {p.id for p in profiles_mod.default_profiles()}

        assert any(i.startswith("nim-") for i in ids)
        assert any(i.startswith("zen-") for i in ids)

    def test_no_local_profile_hardcodes_a_model_name(self, monkeypatch):
        """
        Same rule as the broker's registry: a model name written here would be
        a guess about someone else's machine.
        """
        monkeypatch.setenv(local_mod.ENV_LOCAL_ONLY, "1")
        monkeypatch.delenv("EVOLUTION_LOCAL_MODEL", raising=False)
        from control_plane.providers import profiles as profiles_mod

        for profile in profiles_mod.default_profiles():
            assert profile.model == "", (
                f"{profile.id} ships the model name {profile.model!r}")


class TestTheReportedChainIsTheRealChain:
    """
    The Models page renders the broker's chain under the words "the chain the
    broker will actually try, in order".

    In local-only mode it read `nvidia_nim/... -> opencode_zen/...` — providers
    that are never constructed in that process. Routing was not wrong (those
    entries are skipped as "provider not configured" and the discovered local
    routes serve), but the UI stated an order that cannot happen, to an operator
    whose reason for reading that page is to confirm their run is offline.

    A displayed chain that is not the chain is the no-fabricated-data rule
    broken exactly where it matters most.
    """

    def test_the_starting_chain_is_empty_in_local_only(self, monkeypatch):
        monkeypatch.setenv(local_mod.ENV_LOCAL_ONLY, "1")
        import importlib

        router = importlib.import_module("oe_max.router")

        assert router.default_chain() == [], (
            "the broker would report cloud routes it cannot use")

    def test_the_cloud_chain_is_unchanged_when_the_switch_is_off(self, monkeypatch):
        monkeypatch.delenv(local_mod.ENV_LOCAL_ONLY, raising=False)
        import importlib

        router = importlib.import_module("oe_max.router")
        chain = router.default_chain()

        assert chain, "the normal chain must not be emptied"
        assert chain[0][0] == "nvidia_nim", "NIM still leads outside local mode"

    def test_the_broker_seeds_from_default_chain_not_the_constant(self):
        """
        The bug was that BrokerState hardcoded `list(DEFAULT_CHAIN)`, so the
        mode-aware function could never take effect.
        """
        import inspect

        from oe_max.broker import app as broker_app

        source = inspect.getsource(broker_app.BrokerState.__init__)

        assert "default_chain()" in source
        assert "DEFAULT_CHAIN" not in source, (
            "the broker is seeding from the constant again, which ignores "
            "local-only mode")


class TestVerifyIsAffordableLocally:
    """
    `--verify` smoke-tests every model with a real completion, twice — once
    plain and once with a tools payload.

    On a remote provider that is instant. On a local 27B at ~3 tok/s a
    200-token probe is about a minute, so five discovered local routes is ten
    probes and roughly ten minutes, plus a model reload whenever the probe
    moves between models a 16 GB box can only hold one of. Verification then
    takes longer than the evolution run it was meant to precede, and the
    natural response is to stop verifying — which is the opposite of what the
    two-stage discovery is for.

    Nothing is lost by shrinking it: `reachable` is decided by HTTP 200, not by
    what the model said.
    """

    def test_local_probes_use_a_small_budget(self, fresh_registry):
        providers = fresh_registry(OE_MAX_LOCAL_ONLY="1")

        for name, adapter in providers.items():
            budget = getattr(adapter, "probe_max_tokens", None)
            assert budget is not None, f"{name} has no probe budget"
            assert budget <= 32, (
                f"{name} probes with {budget} tokens; at local generation "
                "rates that makes --verify slower than the run")

    def test_a_provider_without_one_keeps_the_default(self, fresh_registry):
        """
        Remote providers are unaffected: their tokens are fast, and a reasoning
        model there may genuinely need headroom to answer at all.
        """
        providers = fresh_registry(OE_MAX_LOCAL_ONLY=None)
        nim = providers["nvidia_nim"]

        assert getattr(nim, "probe_max_tokens", None) is None

    def test_the_probe_reads_the_budget_from_the_provider(self):
        """A setting the probe does not consult is not a setting."""
        import inspect

        from oe_max.providers.registry import Registry

        source = inspect.getsource(Registry.probe_model)

        assert "probe_max_tokens" in source
        assert source.count("probe_tokens") >= 3, (
            "both the plain probe and the tools probe must use it")


class TestRolesShareOneResidentModel:
    """
    Every role must select the same local model.

    On a box that can hold exactly one 16 GB model, a role whose preference
    differs from the previous request's forces an unload and a reload. Measured
    on this hardware that is 20-90 seconds — against a mutation that takes 165
    seconds, so a judge call routed to a different model roughly doubles the
    cost of the iteration, and does it invisibly: every request still succeeds,
    the run is just mysteriously slower than the token rate predicts.

    Today this holds because the whole chain comes from discovery and the first
    discovered route wins for every role. That is a property of the current
    ordering rather than a decision anyone made, which is exactly the kind of
    thing that stops being true without anyone noticing.
    """

    def _router_with_discovered_models(self):
        from oe_max.providers.base import ModelSpec
        from oe_max.providers.registry import Registry, build_default_registry
        from oe_max.router import Router, default_chain

        registry = Registry(build_default_registry())
        registry.providers["ollama"].models = {
            "model_a": ModelSpec(key="model_a", id="a:latest", priority=100),
            "model_b": ModelSpec(key="model_b", id="b:latest", priority=100),
        }
        registry.providers["lmstudio"].models = {
            "model_c": ModelSpec(key="model_c", id="c", priority=100),
        }
        router = Router(registry, chain=default_chain())
        router.refresh_chains()
        return router

    def test_every_role_selects_the_same_model(self, monkeypatch):
        monkeypatch.setenv(local_mod.ENV_LOCAL_ONLY, "1")
        from oe_max.roles import Role

        router = self._router_with_discovered_models()

        chosen = set()
        for role in Role:
            routes, _ = router.candidates(role=role)
            assert routes, f"{role.value} has no route"
            chosen.add((routes[0].provider, routes[0].model_key))

        assert len(chosen) == 1, (
            f"roles select {len(chosen)} different models: {chosen}. On a "
            "single-model box each switch is a full reload, which silently "
            "doubles the cost of an iteration")

    def test_a_second_model_is_still_reachable_as_a_fallback(self, monkeypatch):
        """
        Sharing one model must not mean the others are unroutable — if the
        preferred one starts failing, the chain still has somewhere to go.
        Preference, not exclusion.
        """
        monkeypatch.setenv(local_mod.ENV_LOCAL_ONLY, "1")
        from oe_max.roles import Role

        router = self._router_with_discovered_models()
        routes, _ = router.candidates(role=Role.REASONER)

        assert len(routes) > 1, "no fallback if the preferred model degrades"
