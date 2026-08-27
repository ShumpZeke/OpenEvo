"""
BrainPort — provider-neutral intelligence abstraction.

OpenEvo evolution owns:
  search, mutation strategies, candidate requests, parent selection,
  operator/bandit policies, archives, novelty, Pareto, failure memory,
  deterministic gates, evaluation scheduling, benchmarks, isolation,
  checkpoint/resume, experiment state, lineage, budgets, promotion.

OpenCode owns:
  provider, model, credentials, catalog, reasoning config, harness,
  coding/fs/shell tools, session/context, permissions, model switching.

This package is the hard boundary. No file in this package may import
oe_max.providers, oe_max.router, oe_max.limiter, control_plane.providers,
or contain hardcoded model IDs, provider URLs, or API key env names.
"""

from .capabilities import BrainCapabilities, Capability
from .llm import BrainLLM, NullBrainLLM
from .port import BrainPort
from .types import BrainRequest, BrainResponse, PolicyMode

__all__ = [
    "BrainCapabilities",
    "BrainLLM",
    "BrainPort",
    "BrainRequest",
    "BrainResponse",
    "Capability",
    "NullBrainLLM",
    "PolicyMode",
]
