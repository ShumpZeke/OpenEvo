from __future__ import annotations

from dataclasses import dataclass

from .kernel import ToolResult
from .model import ConversationMessage
from .session import AgentRunStatus


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    status: AgentRunStatus
    output: str
    steps: int
    messages: tuple[ConversationMessage, ...]
    tool_results: tuple[ToolResult, ...]
    profile_ids: tuple[str, ...]
    context_keys: tuple[str, ...]
    session_id: str = ""
    parent_session_id: str | None = None
