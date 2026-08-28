from __future__ import annotations

from dataclasses import dataclass

from ..telemetry.events import Component, Event, EventType, Status
from .kernel import AgentKernel, Goal, ToolCall, ToolResult
from .model import ConversationMessage, MessageRole, ModelToolCall


@dataclass(frozen=True, slots=True)
class ExecutedTool:
    result: ToolResult
    message: ConversationMessage


class AgentToolExecutor:
    def __init__(self, kernel: AgentKernel, max_output_chars: int) -> None:
        self.kernel = kernel
        self.max_output_chars = max_output_chars

    def execute(
        self,
        goal: Goal,
        run_id: str,
        model_call: ModelToolCall,
    ) -> ExecutedTool:
        self.kernel.record(
            Event(
                EventType.AGENT_TOOL_CALLED,
                Component.CONTROL_PLANE,
                trace_id=goal.goal_id,
                run_id=run_id,
                status=Status.RUNNING,
                summary=model_call.name,
                metadata={
                    "tool_call_id": model_call.call_id,
                    "argument_names": [
                        argument.name for argument in model_call.arguments
                    ],
                },
            )
        )
        result = self.kernel.tool(ToolCall(model_call.name, model_call.argument_map()))
        self.kernel.record(
            Event(
                EventType.AGENT_TOOL_COMPLETED,
                Component.CONTROL_PLANE,
                trace_id=goal.goal_id,
                run_id=run_id,
                status=Status.OK if result.succeeded else Status.FAILED,
                duration_ms=result.duration_ms,
                summary=result.tool,
                metadata={
                    "tool_call_id": model_call.call_id,
                    "succeeded": result.succeeded,
                    "error": result.error,
                },
            )
        )
        return ExecutedTool(
            result,
            ConversationMessage(
                MessageRole.TOOL,
                self._message_content(result),
                name=result.tool,
                tool_call_id=model_call.call_id,
            ),
        )

    def _message_content(self, result: ToolResult) -> str:
        content = (
            result.output if result.succeeded else f"ERROR: {result.error or 'unknown'}"
        )
        if len(content) <= self.max_output_chars:
            return content
        return f"{content[: self.max_output_chars]}\n[output truncated]"
