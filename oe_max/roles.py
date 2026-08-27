"""
Role-based routing.

One chain for every request is the wrong shape once the free routes differ in
kind rather than only in quality. Measured on Zen, 2026-08-26:

    nemotron-3-ultra-free    3.3s   39 reasoning tokens on a 2-word answer
    hy3-free                 2.1s   43 reasoning tokens
    laguna-s-2.1-free        1.6s    0 reasoning tokens, prompt cache hit
    nemotron-3.5-lightning   7.6s   64/64 tokens spent reasoning, truncated

With a NIM key the pool widens, measured 2026-08-28:

    nemotron-3-super-120b-a12b   732ms  fastest working route on any provider
    nemotron-3-ultra-550b-a55b   4.5s   reasoning kept OUT of the visible budget
    moonshotai/kimi-k3          11.5s
    deepseek-v4-flash-0731      51.1s   strong, and slow enough to matter

Those are not four grades of the same thing. A reasoning model that spends its
budget thinking is what you want proposing a mutation and precisely what you do
not want ranking two candidates or summarising a diff — there it buys latency
and truncation risk with no gain. Sending every request down one chain means
either paying reasoning cost for clerical work, or doing the reasoning work on
a model that does not reason.

So the chain is per role. Roles are addressed by broker alias, which keeps the
engine untouched: OpenEvolve names a model, and naming `oe-max-judge` selects a
chain rather than a model.

Two properties the composition has to preserve:

  * **No role can be starved.** Each chain is its preference followed by every
    other configured route, so a role whose preferred provider is down or spent
    still serves from whatever remains. A role that 503s while a usable route
    sits idle would be a worse failure than a slow answer.

  * **Preference is not a filter.** The tail is ordered, not excluded. Roles
    express what is *better* here, never what is *permitted* here, because a
    hard filter turns one provider outage into a dead role.
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, List, Tuple

Chain = List[Tuple[str, str]]


class Role(str, Enum):
    """What the request is for. Chosen at the broker by model alias."""

    REASONER = "reasoner"   # proposal/mutation generation — hard reasoning
    CODER = "coder"         # code generation and repository-level edits
    JUDGE = "judge"         # evaluation, ranking, criticism
    FAST = "fast"           # high-volume cheap work: summarising, compressing


# The alias OpenEvolve is configured with. Kept pointing at REASONER so an
# existing config keeps its current behaviour: mutation generation is what it
# was always doing.
PRIMARY_ALIAS = "oe-max-primary"

ALIASES: Dict[str, Role] = {
    PRIMARY_ALIAS: Role.REASONER,
    "oe-max-reasoner": Role.REASONER,
    "oe-max-coder": Role.CODER,
    "oe-max-judge": Role.JUDGE,
    "oe-max-fast": Role.FAST,
}


# Preferences, most-preferred first. Every entry is (provider, model_key) and
# must exist in the registry; unknown entries are reported, not silently
# dropped (see `validate_preferences`).
#
# NVIDIA NIM leads every chain: it is the operator's chosen provider, and it is
# the only one here whose models were probed individually with a real key
# (HANDOFF 4i -- five of nine serve, and the fastest working route measured on
# any provider, free or paid, is NIM's `nemotron-3-super-120b-a12b` at 732 ms).
#
# Routes that need a credential are filtered out automatically when it is
# absent, so this costs a keyless install nothing: with no NVIDIA_API_KEY the
# NIM entries drop out and the Zen tail serves, exactly as before. With a key,
# NIM is what runs.
_PREFERENCES: Dict[Role, Chain] = {
    # NIM's flagship reasoner first (4.5 s with tools, and its hidden reasoning
    # is kept out of the visible budget, which is what makes it safe for long
    # mutations -- see HANDOFF 3.2 for what the opposite costs). The 120B is the
    # fast second opinion; the Zen tail keeps the role alive with no key.
    Role.REASONER: [
        ("nvidia_nim", "nemotron_ultra_253b"),
        ("nvidia_nim", "nemotron_super_120b"),
        ("opencode_zen", "nemotron_ultra"),
        ("opencode_zen", "hy3"),
    ],
    # Code specialist first now that it is reachable: kimi-k3 is slower (11.5 s)
    # but it is the only route here chosen *for* code. deepseek-v4-flash serves
    # at 51 s, so it sits below the generalist rather than above it.
    Role.CODER: [
        ("nvidia_nim", "kimi_k3"),
        ("nvidia_nim", "nemotron_ultra_253b"),
        ("nvidia_nim", "deepseek_v4_flash"),
        ("opencode_zen", "nemotron_ultra"),
        ("opencode_zen", "hy3"),
    ],
    # Judging wants a consistent, cheap answer rather than hidden reasoning.
    # The 120B is both the cheapest NIM route and the fastest measured anywhere
    # (732 ms), which is exactly the shape this role wants. Zen's laguna stays
    # behind it as the keyless equivalent -- the only free Zen route measured at
    # zero reasoning tokens.
    Role.JUDGE: [
        ("nvidia_nim", "nemotron_super_120b"),
        ("nvidia_nim", "nemotron_nano_30b"),
        ("opencode_zen", "laguna"),
        ("opencode_zen", "nemotron_ultra"),
    ],
    # Fast work is latency-bound, so this is the one role where the ordering is
    # purely the measured number: 732 ms, then the nano, then the keyless tail.
    # `nemotron_lightning` is deliberately absent despite the name -- measured at
    # 7.6 s, the slowest of the four free Zen routes, because it reasons first.
    Role.FAST: [
        ("nvidia_nim", "nemotron_super_120b"),
        ("nvidia_nim", "nemotron_nano_30b"),
        ("opencode_zen", "laguna"),
        ("opencode_zen", "hy3"),
    ],
}


def role_for_alias(model: str) -> Role:
    """
    Which role a requested model name selects.

    Unknown names fall to REASONER rather than erroring: a client that has not
    been told about the aliases must still get served, and mutation generation
    is the safe default because it is what the engine asks for by default.
    """
    return ALIASES.get((model or "").strip().lower(), Role.REASONER)


def build_chains(all_routes: Chain) -> Dict[Role, Chain]:
    """
    Full chain per role: preference first, then everything else.

    `all_routes` is the globally-ordered route list — normally the registry's
    own priority order — and supplies the tail. Passing it in rather than
    reading the registry here keeps this module free of provider knowledge and
    makes the composition testable without a registry at all.
    """
    chains: Dict[Role, Chain] = {}
    for role in Role:
        seen = set()
        chain: Chain = []
        for entry in list(_PREFERENCES.get(role, [])) + list(all_routes):
            if entry in seen:
                continue
            seen.add(entry)
            chain.append(entry)
        chains[role] = chain
    return chains


def preferences() -> Dict[Role, Chain]:
    """The declared preferences, without the shared tail."""
    return {role: list(chain) for role, chain in _PREFERENCES.items()}


def validate_preferences(known: Chain) -> Dict[str, List[str]]:
    """
    Which preferred routes do not exist in the registry.

    A preference naming a model that was renamed or withdrawn degrades
    silently — the entry is skipped and the role quietly runs on its tail,
    which looks like a working system doing the wrong thing. This makes that
    visible; the broker reports it at startup.
    """
    known_set = set(known)
    missing: Dict[str, List[str]] = {}
    for role, chain in _PREFERENCES.items():
        gone = [f"{p}/{m}" for p, m in chain if (p, m) not in known_set]
        if gone:
            missing[role.value] = gone
    return missing
