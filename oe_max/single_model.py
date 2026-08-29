"""Single-model mode: one model answers everything, and the operator picks it.

Normally each role -- reasoner, coder, judge, fast -- has its own chain, and a
request is routed by the alias it names. That is the right default: the roles
genuinely want different models, and failover keeps a run alive when one route
dies.

It is the wrong shape for three things an operator actually does:

* **Judging a model.** "Is kimi-k3 any good here?" cannot be answered by a run
  where three other models also served requests.
* **Comparing two.** Every arm of an A/B has to be one model or the comparison
  measures the chain.
* **Just wanting a specific model.** The operator has one they like. Nothing
  should have to argue with them about it.

So this is a mode, not a policy change. Off by default; when on, every request
resolves to the chosen route and nothing else.

## It pins, and a pin fails rather than substituting

If the chosen model is not usable, requests fail with a message naming it. They
do **not** quietly fall back to a chain. Silent substitution is the failure this
mode exists to prevent -- a run that reports "single model: kimi-k3" while three
others answered is worse than a run that stops.

That follows the precedent already set by per-request pinning in the broker,
where naming a concrete model means that route and no failover.

## Matching is by substring, deliberately

`OE_MAX_LOCAL_MODELS` already works this way and for the same reason: an
operator types `kimi`, not `moonshotai/kimi-k3`. A query that matches more than
one route is refused rather than guessed at -- picking one silently is how you
end up running a model you did not choose.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .router import Route

__all__ = [
    "ENV_SINGLE_MODEL",
    "Candidate",
    "active_route",
    "candidates",
    "clear",
    "describe",
    "resolve",
    "selection",
    "select",
]

# Seeds the mode at process start. Set it and every request goes to that model.
ENV_SINGLE_MODEL = "OE_MAX_SINGLE_MODEL"

_lock = threading.Lock()
_selection: Optional[str] = None
_seeded = False


@dataclass(frozen=True)
class Candidate:
    """One route the operator could pick, as the picker needs to show it."""

    provider: str
    model_key: str
    model_id: str
    available: Optional[bool]
    notes: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "model_key": self.model_key,
            "model_id": self.model_id,
            # `None` means "not probed", which is not the same as False and must
            # not be rendered as one.
            "available": self.available,
            "notes": self.notes,
            "label": "{}/{}".format(self.provider, self.model_id),
        }


def candidates(registry: Any) -> List[Candidate]:
    """
    Every route the operator could choose, for the picker.

    Includes models marked unavailable, with the flag, rather than hiding them:
    a model that is disabled for a recorded reason is exactly what someone wants
    to see when asking "why can I not pick this". Providers with no usable
    credential are excluded, because picking one produces a route that cannot
    serve and a confusing failure rather than an obvious one.
    """
    out: List[Candidate] = []
    for provider in registry.providers.values():
        if not provider.usable():
            continue
        for spec in provider.models.values():
            out.append(Candidate(
                provider=provider.name,
                model_key=spec.key,
                model_id=spec.id,
                available=getattr(spec, "available", None),
                notes=getattr(spec, "notes", "") or "",
            ))
    out.sort(key=lambda c: (c.provider, c.model_id))
    return out


def resolve(registry: Any, query: str) -> Tuple[Optional[Route], str]:
    """
    Turn what the operator typed into exactly one route.

    Returns `(route, reason)`. `route` is None when the query does not select
    exactly one thing, and `reason` says which of the two ways it failed --
    nothing matched, or several did. Both are worth distinguishing: the first is
    a typo or a withdrawn model, the second is a query that needs narrowing, and
    telling someone "no route" when they meant to be more specific wastes their
    time.

    Exact matches on `model_id` or `model_key` win outright, so a query that is
    an exact id is never ambiguous even if it is a substring of another.
    """
    needle = (query or "").strip().lower()
    if not needle:
        return None, "no model selected"

    exact: List[Candidate] = []
    partial: List[Candidate] = []
    for candidate in candidates(registry):
        if needle in (candidate.model_id.lower(), candidate.model_key.lower()):
            exact.append(candidate)
        elif needle in candidate.model_id.lower() or needle in candidate.model_key.lower():
            partial.append(candidate)

    hits = exact or partial
    if not hits:
        return None, "no configured model matches {!r}".format(query)
    if len(hits) > 1:
        return None, "{!r} matches {} models ({}); be more specific".format(
            query, len(hits), ", ".join(sorted(c.model_id for c in hits)[:6]))

    hit = hits[0]
    return Route(provider=hit.provider, model_key=hit.model_key,
                 model_id=hit.model_id), "ok"


def _seed_from_env() -> None:
    """Read the environment once, on first use.

    Once rather than per call, so that clearing the mode at runtime is not undone
    by the next request re-reading a variable the operator has moved on from.
    """
    global _selection, _seeded
    if _seeded:
        return
    _seeded = True
    value = (os.environ.get(ENV_SINGLE_MODEL) or "").strip()
    if value:
        _selection = value


def selection() -> Optional[str]:
    """What the operator asked for, verbatim, or None when the mode is off."""
    with _lock:
        _seed_from_env()
        return _selection


def select(query: Optional[str]) -> None:
    """Turn the mode on (or off, with a falsy query). Does not validate.

    Validation belongs to the caller, which has a registry and can say *why* a
    query is no good. Storing an unresolvable string on purpose keeps the mode
    honest: `describe()` will report it as selected-but-unresolvable rather than
    silently behaving as though the mode were off.
    """
    global _selection, _seeded
    with _lock:
        _seeded = True
        _selection = (query or "").strip() or None


def clear() -> None:
    """Back to role chains."""
    select(None)


def active_route(registry: Any) -> Tuple[Optional[Route], str]:
    """
    The route every request should use, or `(None, reason)`.

    `(None, "off")` specifically means the mode is off and normal role routing
    applies. Any other reason means the mode is ON and cannot be satisfied,
    which the caller must treat as an error rather than as a licence to fall
    back to a chain.
    """
    current = selection()
    if current is None:
        return None, "off"
    return resolve(registry, current)


def describe(registry: Any) -> Dict[str, Any]:
    """The mode's state, for the status endpoint and the picker."""
    current = selection()
    if current is None:
        return {"enabled": False, "selected": None, "route": None, "reason": "off"}
    route, reason = resolve(registry, current)
    return {
        "enabled": True,
        "selected": current,
        "route": None if route is None else {
            "provider": route.provider,
            "model_key": route.model_key,
            "model_id": route.model_id,
        },
        # Non-"ok" while enabled is a fault, not a fallback: every request will
        # fail until the selection is fixed or cleared.
        "reason": reason,
        "ok": route is not None,
    }


def reset_for_tests() -> None:
    """Forget the selection *and* that the environment was read."""
    global _selection, _seeded
    with _lock:
        _selection = None
        _seeded = False
