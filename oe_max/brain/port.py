"""
BrainPort — abstract interface.

The evolutionary core depends on this abstract type only.

Concrete implementations:
  * LegacyBrainPort  — wraps the existing provider registry/router (temporary)
  * OpenCodeBrainPort / StdioBrainPort — delegates to OpenCode via stdio JSONL (preferred)
  * NullBrainPort / MockBrainPort — for tests and benchmarks

The core must never import a concrete port that contains provider knowledge.
Depend on this file only.
"""

from __future__ import annotations

import abc
from typing import AsyncIterator, Optional

from .capabilities import BrainCapabilities
from .types import BrainRequest, BrainResponse


class BrainPort(abc.ABC):
    """
    Provider-neutral intelligence port.

    The core requests intelligence through this port. The host (OpenCode,
    legacy adapter, or mock) fulfills it with whatever model the user selected
    in OpenCode — brain.mode = inherit.

    Implementations must:
      - not require vendor-specific model identifiers
      - expose capabilities via `capabilities()` (cached per run, not per generation)
      - support cancellation via the request's timeout/abort where possible
      - record provider/model identity only as provenance metadata (not routing)
    """

    @abc.abstractmethod
    async def generate(self, request: BrainRequest) -> BrainResponse:
        """Generate a single completion for the request."""
        raise NotImplementedError

    async def stream(self, request: BrainRequest) -> AsyncIterator[str]:
        """
        Optional streaming. Default implementation buffers generate().
        Hosts that support streaming should override.
        """
        resp = await self.generate(request)
        if resp.ok:
            yield resp.content
        else:
            raise RuntimeError(resp.error or "brain stream failed")

    async def capabilities(self) -> BrainCapabilities:
        """
        Host-reported capabilities for the active model/session.
        Cached per run — not rediscovered per generation.
        Default: minimal (text only) — hosts should override.
        """
        return BrainCapabilities.minimal()

    async def health_check(self) -> bool:
        """Lightweight liveness probe. Return True if the brain is reachable."""
        try:
            caps = await self.capabilities()
            return caps.text
        except Exception:
            return False

    async def close(self) -> None:
        """Release resources. Called on shutdown/restart."""
        return None


class BrainPortError(RuntimeError):
    """Typed error for brain failures, distinct from candidate failures."""

    def __init__(self, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class NullBrainPort(BrainPort):
    """For tests that don't need a real brain — returns a deterministic stub."""

    def __init__(self, stub: str = "# stub: no brain configured\n") -> None:
        self.stub = stub
        self.calls: int = 0

    async def generate(self, request: BrainRequest) -> BrainResponse:
        self.calls += 1
        return BrainResponse(content=self.stub, usage={"stub": 1})
