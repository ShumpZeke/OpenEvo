"""
The provider catalogue: providers as configuration.

Three providers were hardcoded here, which made adding a fourth a code change
and made the set impossible for an operator to adjust. `configs/oe_max/
providers.yaml` replaces that. Every entry is key-gated, so declaring fifteen
providers costs nothing at runtime: one without its credential is not usable
and never reaches a chain.

The part worth understanding is how concrete model ids get created, because
this project has been burned three times by ids written from memory —
`x-preview-f-free` (withdrawn), and `deepseek-ai/deepseek-v4-pro` and
`qwen/qwen2.5-coder-32b-instruct` (never in NVIDIA's catalogue at all).

So the catalogue never names a model. It names *patterns of interest*, and
models are materialised from the provider's own live listing at discovery time.
A pattern that matches nothing yields no routes and no error, which is the
correct outcome: it is a preference that went unsatisfied, not a fault. The
consequence worth stating plainly is that a mistyped pattern fails silently and
a mistyped model id would not — that trade is deliberate, because a silent
absence costs one unused provider while a confident wrong id costs every
request routed to it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

from ..limiter import NullLimiter
from .base import ModelSpec, ProviderAdapter, ProviderRole

DEFAULT_CATALOGUE = os.path.join("configs", "oe_max", "providers.yaml")


@dataclass
class FreeTier:
    """
    What we believe about a provider's free access, and how much to trust it.

    `confidence` is not decoration. "documented" and "verified" are different
    claims, and a UI that renders both as "free" would be asserting something
    nobody checked. Nothing here is `verified` for a provider we have no key
    for, and no code path may upgrade it.
    """

    status: str = "unverified"      # free_recurring | signup_credits | paid | unverified
    note: str = ""
    source: str = ""
    confidence: str = "unverified"  # verified | catalogue-verified | documented | unverified

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class RetiredEntry:
    """A provider or model that was checked and found dead."""

    name: str
    base_url: str
    finding: str
    verified: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class CatalogueEntry:
    name: str
    base_url: str
    api_key_env: Optional[str] = None
    requires_key: bool = True
    public_listing: bool = False
    timeout_s: float = 120.0
    liveness: str = ""
    free_tier: FreeTier = field(default_factory=FreeTier)
    prefer_patterns: List[str] = field(default_factory=list)
    max_models: int = 5
    enabled: bool = True


def load_catalogue(path: Optional[str] = None) -> Dict[str, Any]:
    """
    Read the catalogue. A missing file is not an error.

    The built-in providers must keep working when the file is absent or
    unreadable — a broker that refuses to start because an optional catalogue
    is missing would be a worse failure than running without the extra
    providers it would have added.
    """
    path = path or os.environ.get("OE_MAX_PROVIDER_CATALOGUE", DEFAULT_CATALOGUE)
    if not os.path.exists(path):
        return {"providers": [], "retired": [], "path": path, "loaded": False}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError) as exc:
        return {"providers": [], "retired": [], "path": path, "loaded": False,
                "error": f"{type(exc).__name__}: {exc}"[:200]}

    entries: List[CatalogueEntry] = []
    for item in raw.get("providers") or []:
        ft = item.get("free_tier") or {}
        entries.append(CatalogueEntry(
            name=item["name"],
            base_url=item["base_url"],
            api_key_env=item.get("api_key_env"),
            requires_key=bool(item.get("requires_key", True)),
            public_listing=bool(item.get("public_listing", False)),
            timeout_s=float(item.get("timeout_s", 120.0)),
            liveness=item.get("liveness", ""),
            free_tier=FreeTier(
                status=ft.get("status", "unverified"),
                note=ft.get("note", ""),
                source=ft.get("source", ""),
                confidence=ft.get("confidence", "unverified"),
            ),
            prefer_patterns=list(item.get("prefer_patterns") or []),
            max_models=int(item.get("max_models", 5)),
            enabled=bool(item.get("enabled", True)),
        ))

    retired = [
        RetiredEntry(name=r["name"], base_url=r.get("base_url", ""),
                     finding=r.get("finding", ""), verified=r.get("verified", ""))
        for r in (raw.get("retired") or [])
    ]
    return {"providers": entries, "retired": retired, "path": path, "loaded": True}


def build_provider(entry: CatalogueEntry) -> ProviderAdapter:
    """One catalogue entry as an adapter. Models stay empty until discovery."""
    adapter = ProviderAdapter(
        name=entry.name,
        base_url=entry.base_url,
        role=ProviderRole.SPECIALIST_AND_FALLBACK,
        api_key_env=entry.api_key_env,
        limiter=NullLimiter(entry.name),
        requires_key=entry.requires_key,
        public_listing=entry.public_listing,
        timeout_s=entry.timeout_s,
        enabled=entry.enabled,
        models={},
    )
    # Carried on the adapter so discovery can materialise models without
    # having to hold the catalogue too.
    adapter.prefer_patterns = list(entry.prefer_patterns)
    adapter.max_models = entry.max_models
    adapter.free_tier = entry.free_tier
    adapter.liveness = entry.liveness
    return adapter


def _model_key(model_id: str) -> str:
    """A stable local handle for a discovered id."""
    return re.sub(r"[^a-z0-9]+", "_", model_id.lower()).strip("_")[:64]


def materialise_models(
    adapter: ProviderAdapter, listed: List[str],
) -> Dict[str, ModelSpec]:
    """
    Turn a provider's live listing into ranked ModelSpecs.

    Ordering follows the position of the first matching pattern, so the
    catalogue expresses preference without naming anything. Within a pattern,
    the provider's own listing order is kept — we have no basis for a better
    one, and inventing a tie-break would look like a judgement we did not make.

    Embedding, reranking, guard, vision and transcription models are dropped:
    they answer a different API shape, and routing a mutation to an embedding
    model produces a confusing failure rather than an obvious one.
    """
    patterns = adapter.prefer_patterns or []
    if not patterns:
        return {}

    excluded = re.compile(
        r"embed|rerank|guard|safety|moderation|whisper|tts|stable-diffusion|"
        r"vision|image|video|ocr|parse|translate|nemoretriever|nvclip",
        re.I,
    )
    compiled = [(i, re.compile(p, re.I)) for i, p in enumerate(patterns)]

    ranked: List[tuple] = []
    for order, model_id in enumerate(listed):
        if not model_id or excluded.search(model_id):
            continue
        for rank, rx in compiled:
            if rx.search(model_id):
                ranked.append((rank, order, model_id))
                break

    ranked.sort(key=lambda t: (t[0], t[1]))
    out: Dict[str, ModelSpec] = {}
    for rank, _, model_id in ranked[: max(0, adapter.max_models)]:
        key = _model_key(model_id)
        out[key] = ModelSpec(
            key=key, id=model_id,
            # Higher priority for earlier-matching patterns, so the chain tail
            # is ordered by the operator's stated preference.
            priority=max(1, 100 - rank * 10),
            notes=(f"discovered from {adapter.name}'s live listing "
                   f"(pattern rank {rank}); inference unverified until probed"),
        )
    return out
