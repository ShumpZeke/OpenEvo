from __future__ import annotations

import json
import os
from typing import TypeAlias

import httpx2
from pydantic import BaseModel, ConfigDict, Field, RootModel, ValidationError

from ..providers.profiles import ModelProfile
from .http_client import create_http_client
from .model import (
    ConversationMessage,
    MessageRole,
    ModelInvocationError,
    ModelRequest,
    ModelResponse,
    ModelToolArgument,
    ModelToolCall,
    ModelToolDefinition,
)

JsonValue: TypeAlias = (
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
)
JsonObject: TypeAlias = dict[str, JsonValue]
WireArgumentValue: TypeAlias = str | int | float | bool | None


class _WireArguments(RootModel[dict[str, WireArgumentValue]]):
    model_config = ConfigDict(frozen=True)


class _WireFunction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    arguments: str = "{}"


class _WireToolCall(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str
    function: _WireFunction


class _WireMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    content: str | None = None
    tool_calls: tuple[_WireToolCall, ...] | None = None


class _WireChoice(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    message: _WireMessage
    finish_reason: str | None = None


class _WireUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    prompt_tokens: int = 0
    completion_tokens: int = 0


class _WireCompletion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    choices: tuple[_WireChoice, ...] = Field(min_length=1)
    model: str = ""
    usage: _WireUsage | None = None


class OpenAICompatibleProvider:
    def complete(self, profile: ModelProfile, request: ModelRequest) -> ModelResponse:
        payload = _request_payload(profile, request)
        headers = {"Content-Type": "application/json"}
        secret = os.environ.get(profile.secret_ref) if profile.secret_ref else None
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        try:
            with create_http_client(
                profile.api_base,
                profile.timeout_s,
                profile.max_retries,
                headers,
            ) as client:
                response = client.post("chat/completions", json=payload)
        except httpx2.HTTPError as exc:
            raise ModelInvocationError(profile.id, type(exc).__name__) from exc
        if response.status_code >= 400:
            raise ModelInvocationError(
                profile.id,
                f"HTTP {response.status_code}",
                rate_limited=response.status_code == 429,
            )
        try:
            completion = _WireCompletion.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise ModelInvocationError(
                profile.id, "invalid completion response"
            ) from exc
        choice = completion.choices[0]
        tool_calls = tuple(
            _parse_tool_call(profile.id, call)
            for call in (choice.message.tool_calls or ())
        )
        usage = completion.usage or _WireUsage()
        return ModelResponse(
            content=choice.message.content or "",
            finish_reason=choice.finish_reason
            or ("tool_calls" if tool_calls else "stop"),
            tool_calls=tool_calls,
            model=completion.model or profile.model,
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
        )


def _request_payload(profile: ModelProfile, request: ModelRequest) -> JsonObject:
    payload: JsonObject = {
        "model": profile.model,
        "messages": [_message_payload(message) for message in request.messages],
    }
    if request.tools:
        payload["tools"] = [_tool_payload(tool) for tool in request.tools]
        payload["tool_choice"] = "auto"
    max_tokens = request.max_output_tokens or profile.max_output_tokens
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    temperature = (
        request.temperature if request.temperature is not None else profile.temperature
    )
    if temperature is not None:
        payload["temperature"] = temperature
    if profile.top_p is not None:
        payload["top_p"] = profile.top_p
    return payload


def _message_payload(message: ConversationMessage) -> JsonObject:
    payload: JsonObject = {"role": message.role.value}
    if message.role is MessageRole.ASSISTANT and message.tool_calls:
        payload["content"] = message.content or None
        payload["tool_calls"] = [
            {
                "id": call.call_id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(
                        call.argument_map(), separators=(",", ":"), sort_keys=True
                    ),
                },
            }
            for call in message.tool_calls
        ]
        return payload
    payload["content"] = message.content
    if message.name is not None:
        payload["name"] = message.name
    if message.tool_call_id is not None:
        payload["tool_call_id"] = message.tool_call_id
    return payload


def _tool_payload(tool: ModelToolDefinition) -> JsonObject:
    properties: JsonObject = {}
    required: list[JsonValue] = []
    for parameter in tool.parameters:
        properties[parameter.name] = {
            "type": parameter.json_type,
            "description": parameter.description,
        }
        if parameter.required:
            required.append(parameter.name)
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


def _parse_tool_call(profile_id: str, call: _WireToolCall) -> ModelToolCall:
    try:
        raw_arguments = _WireArguments.model_validate(
            json.loads(call.function.arguments)
        ).root
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ModelInvocationError(profile_id, "invalid tool arguments JSON") from exc
    arguments = tuple(
        ModelToolArgument(name, _argument_text(value))
        for name, value in raw_arguments.items()
    )
    return ModelToolCall(call.id, call.function.name, arguments)


def _argument_text(value: WireArgumentValue) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), sort_keys=True)
