"""
Local OpenAI-compatible servers, and the switch that guarantees offline.

WHY THIS EXISTS. Every provider in this repository was a remote endpoint behind
a credential. The one nod to running locally was a single disabled profile in
the control plane and `scripts/local_provider.py`, which is a stub that replays
a fixed pool of five diffs and never reads the prompt. Neither lets a real model
on this machine drive an evolution run: the broker — the thing OpenEvolve
actually talks to — had no local provider at all.

The four servers below cover what people actually run. All of them speak the
OpenAI protocol, which is the only thing the broker requires, so none of them
needs a new adapter:

    ollama     127.0.0.1:11434   the common default
    lmstudio   127.0.0.1:1234    LM Studio's server tab
    vllm       127.0.0.1:8000    vLLM's OpenAI server
    llamacpp   127.0.0.1:8080    llama.cpp's `llama-server`

TWO THINGS THIS DELIBERATELY DOES NOT DO.

**It writes down no model ids.** Rule 6 says ids are discovered, not
remembered, and local is where that rule is least negotiable: what a machine
serves is whatever its operator pulled. `prefer_patterns=["."]` accepts the
listing as-is, and `materialise_models` still drops embedding, reranking and
vision models, which answer a different API shape.

**It asks for no key.** A local server needs none, so `requires_key=False`
keeps these routes usable in a checkout with no credentials at all — which is
the entire point of local mode.

The base URLs are environment-overridable because ports move: a second Ollama,
a vLLM on a spare GPU box, a llama.cpp behind a reverse proxy. Pointing
`OE_MAX_OLLAMA_BASE` at another host is supported and is *not* a contradiction
of offline mode: offline here means "no route this project configured to a
commercial provider", not "no sockets".
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional

from ..limiter import NullLimiter
from .base import ProviderAdapter, ProviderRole

# Set to 1/true/yes/on to refuse every non-local route.
ENV_LOCAL_ONLY = "OE_MAX_LOCAL_ONLY"

# An ordered, comma-separated list of model-id substrings, most preferred
# first. Nothing here ranks local models -- see _preferred_patterns for why
# this is the operator's statement rather than ours.
ENV_PREFER_MODELS = "OE_MAX_LOCAL_MODELS"

# Local generation is slow in a way remote generation is not: a 30B model on
# CPU can spend several minutes on one mutation, and that is working correctly
# rather than hanging. A short ceiling here would manufacture timeouts and then
# blame the model. See docs/gotchas.md — this moves with max_tokens.
DEFAULT_TIMEOUT_S = 1800.0

# (name, environment variable, default base URL, human name)
LOCAL_SERVERS = (
    ("ollama", "OE_MAX_OLLAMA_BASE", "http://127.0.0.1:11434/v1", "Ollama"),
    ("lmstudio", "OE_MAX_LMSTUDIO_BASE", "http://127.0.0.1:1234/v1", "LM Studio"),
    ("vllm", "OE_MAX_VLLM_BASE", "http://127.0.0.1:8000/v1", "vLLM"),
    ("llamacpp", "OE_MAX_LLAMACPP_BASE", "http://127.0.0.1:8080/v1", "llama.cpp"),
)

LOCAL_PROVIDER_NAMES = tuple(name for name, _, _, _ in LOCAL_SERVERS)


def local_only(env: Optional[Dict[str, str]] = None) -> bool:
    """
    Whether this process must refuse every non-local route.

    Read at registry construction rather than at request time on purpose: a
    guarantee that can change mid-run is not a guarantee, and "did any request
    leave this machine?" should be answerable from how the process started.
    """
    source = os.environ if env is None else env
    return str(source.get(ENV_LOCAL_ONLY, "")).strip().lower() in (
        "1", "true", "yes", "on",
    )


def base_url_for(name: str, env: Optional[Dict[str, str]] = None) -> str:
    """The configured base URL for one local server."""
    source = os.environ if env is None else env
    for server, var, default, _ in LOCAL_SERVERS:
        if server == name:
            return (source.get(var) or default).rstrip("/")
    raise KeyError(f"unknown local server: {name!r}")


def is_local_provider(name: str) -> bool:
    return name in LOCAL_PROVIDER_NAMES


def _preferred_patterns(env: Optional[Dict[str, str]] = None) -> List[str]:
    """
    The operator's stated model order, as patterns for ``materialise_models``.

    ``OE_MAX_LOCAL_MODELS`` is a comma-separated list, most-preferred first::

        OE_MAX_LOCAL_MODELS="qwen-evo-text,qwen3:0.6b"

    Each entry is a *substring* of the model id, matched case-insensitively and
    escaped, so ``qwen3:0.6b`` means what it looks like rather than being read
    as a regex -- a model id full of dots and colons is exactly the input that
    would otherwise misbehave.

    Unset means the list is empty and the catch-all alone survives, which is the
    behaviour this had before. Anything not named keeps the server's own listing
    order *behind* everything named, rather than being dropped: a chain that can
    empty itself is worse than one ordered imperfectly, and an operator who
    names a model they have since deleted should get a working run rather than a
    silent outage.
    """
    import re

    source = os.environ if env is None else env
    raw = str(source.get(ENV_PREFER_MODELS, "") or "")
    return [re.escape(item.strip()) for item in raw.split(",") if item.strip()]


def build_local_providers(
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, ProviderAdapter]:
    """
    Adapters for every local server, whether or not one is currently running.

    They are declared unconditionally and discovered later. A server that is
    not up simply lists nothing and contributes no routes — the same shape as a
    provider whose credential is absent — which keeps "is Ollama running?" a
    question answered by a probe rather than by import-time guesswork.
    """
    out: Dict[str, ProviderAdapter] = {}
    for name, _var, _default, label in LOCAL_SERVERS:
        adapter = ProviderAdapter(
            name=name,
            base_url=base_url_for(name, env),
            role=ProviderRole.SPECIALIST_AND_FALLBACK,
            # No credential. Not "the variable is unset" — there is none.
            api_key_env=None,
            requires_key=False,
            # A local /v1/models needs no authorization, so the listing is
            # readable exactly when the server is up. That makes discovery the
            # liveness check as well, with no extra request.
            public_listing=True,
            limiter=NullLimiter(name),
            timeout_s=timeout_s,
            models={},
        )
        # "." matches any id: what this machine serves is whatever was pulled,
        # and a pattern list cannot anticipate it. materialise_models still
        # excludes embedding/vision/rerank models.
        #
        # OE_MAX_LOCAL_MODELS lets the operator put an order on that. Nothing
        # here ranks local models -- a machine's "best" depends on its hardware
        # and what was pulled, and inventing a ranking would read as a
        # measurement nobody took. But an operator who has tuned one model and
        # kept eleven experiments beside it *does* know, and had no way to say
        # so: the chain took the server's listing order, so a 1 GB experiment
        # could lead a 27B that was tuned for the box.
        adapter.prefer_patterns = _preferred_patterns(env) + ["."]
        # Higher than a remote provider's, because a local box may host many
        # models and none of them cost anything per call.
        adapter.max_models = 12
        # Tell a local reasoning model not to think.
        #
        # Measured on Qwen3.5-27B through Ollama: without this the model
        # answers entirely in `message.reasoning` and returns an EMPTY
        # `content` -- a 400-token budget finished after 16, so it is not
        # truncation, it is the model choosing to think and never answer. With
        # `reasoning_effort: "none"` the same request returns "READY".
        #
        # OpenAI-standard parameter, so a server that does not implement it
        # ignores it rather than failing. Set OE_MAX_LOCAL_REASONING to
        # low/medium/high to keep thinking on where it is wanted -- a judge
        # sometimes should think; a diff generator that thinks instead of
        # answering produces nothing to apply.
        #
        # Read through `env` like `local_only` and `base_url_for` do. Reading
        # `os.environ` directly here meant a caller that passed `env=` got its
        # base URLs honoured and this setting silently ignored.
        source = os.environ if env is None else env
        effort = str(source.get("OE_MAX_LOCAL_REASONING", "none")).strip().lower()
        if effort and effort != "default":
            adapter.extra_body = {"reasoning_effort": effort}
        # A probe only has to establish that the server answers, and the
        # broker decides that on HTTP 200 rather than on the text. 16 tokens
        # is enough to see "ok"; the default 200 costs a minute per probe at
        # local generation rates, twice per model, and made `--verify` slower
        # than the evolution run it precedes.
        adapter.probe_max_tokens = 16
        adapter.liveness = f"{label} — probed at runtime; never assumed"
        out[name] = adapter
    return out


def local_routes(providers: Dict[str, ProviderAdapter]) -> List[tuple]:
    """
    `(provider, model_key)` pairs for every discovered local model.

    Returned in provider-declaration order, then in the order the server itself
    listed them. There is no basis for a better ranking — a local machine's
    "best" model depends on its hardware and what the operator pulled — and
    inventing one would read as a measurement nobody took.
    """
    routes: List[tuple] = []
    for name in LOCAL_PROVIDER_NAMES:
        adapter = providers.get(name)
        if adapter is None:
            continue
        for key in adapter.models:
            routes.append((name, key))
    return routes
