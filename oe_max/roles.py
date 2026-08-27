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
# Routes that need a credential are filtered out automatically when it is
# absent, so a keyless install degrades to the Zen routes rather than failing.
# That is why each preference leads with a route verified to serve keyless.
_PREFERENCES: Dict[Role, Chain] = {
    # Strongest verified-free reasoning first. NIM's flagship sits behind it:
    # better on paper, unverified in this repo, and key-gated.
    Role.REASONER: [
        ("opencode_zen", "nemotron_ultra"),
        ("nvidia_nim", "nemotron_ultra_253b"),
        ("nvidia_nim", "nemotron_super_120b"),
        ("opencode_zen", "hy3"),
    ],
    # Coding leads with the same verified-free route, because a strong general
    # model that answers beats a code-specialised one that needs a key we do
    # not have. The specialists rank above the generalists behind it.
    Role.CODER: [
        ("opencode_zen", "nemotron_ultra"),
        ("nvidia_nim", "kimi_k3"),
        ("nvidia_nim", "nemotron_ultra_253b"),
        ("nvidia_nim", "deepseek_v4_flash"),
        ("opencode_zen", "hy3"),
    ],
    # Judging wants a consistent, cheap, prompt-cacheable answer, not hidden
    # reasoning: laguna is the only free Zen route measured at zero reasoning
    # tokens, so it does this work at a fraction of the cost. The reasoner sits
    # behind it for when the judgement is genuinely hard.
    Role.JUDGE: [
        ("opencode_zen", "laguna"),
        ("nvidia_nim", "nemotron_super_120b"),
        ("opencode_zen", "nemotron_ultra"),
        ("nvidia_nim", "nemotron_nano_30b"),
    ],
    # Fast work is latency-bound. Note `nemotron_lightning` is deliberately NOT
    # first despite the name: measured at 7.6s, the slowest of the four free
    # Zen routes, because it reasons before answering.
    Role.FAST: [
        ("opencode_zen", "laguna"),
        ("nvidia_nim", "nemotron_super_120b"),
        ("nvidia_nim", "nemotron_nano_30b"),
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
