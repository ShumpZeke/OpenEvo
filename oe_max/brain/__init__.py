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

# `.llm` is the one module here that touches upstream: `BrainLLM` subclasses
# `openevolve.llm.base.LLMInterface`, which is how the unmodified engine can be
# handed a BrainPort. Importing it eagerly pulled the whole engine in behind it
# -- `openevolve/__init__` reaches controller -> evaluator -> llm.openai ->
# openai -- so `import oe_max.brain` cost 4.56s, of which 3.0s was an API client
# this package is defined by not using.
#
# Deferred, so the cost falls only on the caller that actually wants the
# adapter. `from oe_max.brain import BrainLLM` still works, and
# `test_importing_brainport_does_not_load_the_engine` fails if anything makes it
# eager again.
_DEFERRED = {"BrainLLM": ".llm", "NullBrainLLM": ".llm"}


def __getattr__(name):
    module_name = _DEFERRED.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value  # subsequent lookups skip this hook entirely
    return value


def __dir__():
    return sorted(__all__)
