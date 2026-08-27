"""
An upstream `LLMInterface` backed by the provider-neutral BrainPort.

This is the adapter that lets the unmodified engine call OpenCode: upstream
asks its configured `LLMInterface` for a completion, and this implementation
answers it from a `BrainPort` instead of a provider client.

`brain.mode = inherit` means the model selected in OpenCode is the model the
engine uses, so there is no provider configuration on this side at all.

It lives here, not under `openevolve/`, because that tree is byte-identical to
upstream and must stay that way -- see PATCH_SURFACE.md. Nothing about this
class needs to be inside it: it subclasses upstream's published interface the
same way any external implementation would.
"""

from __future__ import annotations

from typing import Any, Dict, List

from openevolve.llm.base import LLMInterface


class BrainLLM(LLMInterface):
    """LLMInterface that delegates to a BrainPort."""

    def __init__(self, brain: Any, *, policy: str = "mutation-generation") -> None:
        # brain: BrainPort (typed as Any to avoid import cycle at runtime)
        self.brain = brain
        self.policy = policy
        # For interface compat, expose minimal model-ish attrs
        self.model = "brain/inherit"
        self.temperature = None
        self.max_tokens = None

    async def generate(self, prompt: str, **kwargs) -> str:
        from oe_max.brain.types import BrainRequest, Operation, PolicyMode, Budget

        # Map legacy policy string to PolicyMode
        try:
            policy = PolicyMode(self.policy)
        except ValueError:
            from oe_max.brain.policies import policy_for_legacy_role

            policy = policy_for_legacy_role(self.policy)

        req = BrainRequest(
            operation=Operation.MUTATE,
            objective=prompt,
            context={},
            policy=policy,
            budget=Budget(
                max_tokens=kwargs.get("max_tokens"),
                timeout_s=kwargs.get("timeout"),
            ),
            extra={k: v for k, v in kwargs.items() if k not in ("max_tokens", "timeout")},
        )
        resp = await self.brain.generate(req)
        if not resp.ok:
            raise RuntimeError(resp.error or "brain generate failed")
        if resp.truncated:
            # Surface truncation as retryable — caller may re-ask with larger budget
            raise RuntimeError(resp.error or "output truncated")
        return resp.content

    async def generate_with_context(
        self, system_message: str, messages: List[Dict[str, str]], **kwargs
    ) -> str:
        # Compose system + messages into a single prompt for the brain
        parts: List[str] = []
        if system_message:
            parts.append(system_message)
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            parts.append(f"{role.upper()}: {content}")
        prompt = "\n\n".join(parts)
        return await self.generate(prompt, **kwargs)


class NullBrainLLM(LLMInterface):
    """For tests — returns a deterministic stub without calling any provider."""

    def __init__(self, stub: str = "# generated stub\nprint('hello')\n") -> None:
        self.stub = stub

    async def generate(self, prompt: str, **kwargs) -> str:
        return self.stub

    async def generate_with_context(self, system_message: str, messages: List[Dict[str, str]], **kwargs) -> str:
        return self.stub
