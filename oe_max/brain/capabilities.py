"""
Capability negotiation — model-name-free.

Instead of:
  if model == "<vendor-specific-id>": ...
  if provider == "<vendor-name>": ...

Check:
  if capabilities.has(Capability.STRUCTURED_OUTPUT): ...
  if capabilities.context_limit >= needed: ...

Capabilities are cached per run, not rediscovered per generation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional


class Capability(str, Enum):
    TEXT = "text"
    TOOL_USE = "tool-use"
    STRUCTURED_OUTPUT = "structured-output"
    VISION = "vision"
    STREAMING = "streaming"
    REASONING = "reasoning"
    CANCELLATION = "cancellation"


@dataclass
class BrainCapabilities:
    """
    Capabilities exposed by the host (OpenCode) for the active model/session.

    All fields are host-reported, not inferred from a model ID.
    An unknown capability is None/False, not assumed.
    """

    # Core capabilities — boolean presence
    text: bool = True
    tool_use: bool = False
    structured_output: bool = False
    vision: bool = False
    streaming: bool = False
    reasoning: bool = False
    cancellation: bool = False

    # Limits — host-reported, cached per run
    context_limit: Optional[int] = None      # tokens
    output_limit: Optional[int] = None       # tokens
    reasoning_variants: Optional[list] = None  # e.g. ["low","medium","high"]

    # Raw host metadata, for observability only (not routing logic)
    host_model_id: Optional[str] = None
    host_provider_id: Optional[str] = None
    extra: Dict[str, object] = field(default_factory=dict)

    def has(self, cap: Capability) -> bool:
        mapping = {
            Capability.TEXT: self.text,
            Capability.TOOL_USE: self.tool_use,
            Capability.STRUCTURED_OUTPUT: self.structured_output,
            Capability.VISION: self.vision,
            Capability.STREAMING: self.streaming,
            Capability.REASONING: self.reasoning,
            Capability.CANCELLATION: self.cancellation,
        }
        return bool(mapping.get(cap, False))

    def require(self, cap: Capability) -> None:
        if not self.has(cap):
            raise RuntimeError(f"required capability missing: {cap.value}")

    def to_dict(self) -> Dict[str, object]:
        return {
            "text": self.text,
            "tool_use": self.tool_use,
            "structured_output": self.structured_output,
            "vision": self.vision,
            "streaming": self.streaming,
            "reasoning": self.reasoning,
            "cancellation": self.cancellation,
            "context_limit": self.context_limit,
            "output_limit": self.output_limit,
            "reasoning_variants": self.reasoning_variants,
            "host_model_id": self.host_model_id,
            "host_provider_id": self.host_provider_id,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, object]) -> "BrainCapabilities":
        return cls(
            text=bool(d.get("text", True)),
            tool_use=bool(d.get("tool_use", False)),
            structured_output=bool(d.get("structured_output", False)),
            vision=bool(d.get("vision", False)),
            streaming=bool(d.get("streaming", False)),
            reasoning=bool(d.get("reasoning", False)),
            cancellation=bool(d.get("cancellation", False)),
            context_limit=d.get("context_limit"),  # type: ignore[arg-type]
            output_limit=d.get("output_limit"),  # type: ignore[arg-type]
            reasoning_variants=d.get("reasoning_variants"),  # type: ignore[arg-type]
            host_model_id=d.get("host_model_id"),  # type: ignore[arg-type]
            host_provider_id=d.get("host_provider_id"),  # type: ignore[arg-type]
            extra=dict(d.get("extra") or {}),  # type: ignore[arg-type]
        )

    @classmethod
    def minimal(cls) -> "BrainCapabilities":
        """Fallback when host does not report capabilities."""
        return cls(text=True)
