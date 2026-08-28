from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Mapping

from control_plane.agent import AgentRunResult
from control_plane.telemetry.redaction import Redactor


@dataclass(frozen=True, slots=True)
class NativeTrajectoryRecord:
    candidate_id: str
    parent_id: str
    goal_id: str
    status: str
    agent: AgentRunResult
    verification_accepted: bool
    verification_checks: tuple[str, ...]
    verification_reason: str | None = None
    metrics: Mapping[str, float] = field(default_factory=dict)


def serialize_native_trajectory(
    record: NativeTrajectoryRecord, redactor: Redactor
) -> str:
    clean = redactor.redact_text
    payload = {
        "schema_version": 1,
        "candidate_id": record.candidate_id,
        "parent_id": record.parent_id,
        "goal_id": record.goal_id,
        "status": record.status,
        "verification": {
            "accepted": record.verification_accepted,
            "checks": list(record.verification_checks),
            "reason": record.verification_reason,
        },
        "metrics": {key: float(value) for key, value in record.metrics.items()},
        "agent": {
            "status": record.agent.status.value,
            "steps": record.agent.steps,
            "output": clean(record.agent.output),
            "profiles": list(record.agent.profile_ids),
            "context_keys": list(record.agent.context_keys),
            "messages": [
                {
                    "role": message.role.value,
                    "content": clean(message.content),
                    "name": message.name,
                    "tool_call_id": message.tool_call_id,
                    "tool_calls": [
                        {
                            "call_id": call.call_id,
                            "name": call.name,
                            "arguments": {
                                argument.name: clean(argument.value)
                                for argument in call.arguments
                            },
                        }
                        for call in message.tool_calls
                    ],
                }
                for message in record.agent.messages
            ],
            "tool_calls": [
                {
                    "tool": result.tool,
                    "succeeded": result.succeeded,
                    "output": clean(result.output),
                    "error": clean(result.error or "") or None,
                    "duration_ms": result.duration_ms,
                }
                for result in record.agent.tool_results
            ],
        },
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))
