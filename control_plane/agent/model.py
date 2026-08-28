from __future__ import annotations

import sys
import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import Protocol

from ..providers.profiles import ModelProfile, Role
from ..providers.router import ModelRouter, NoRouteAvailable
from ..telemetry.events import Component, Event, EventType, Status, new_id


class MessageRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class ModelToolParameter:
    name: str
    description: str
    json_type: str = "string"
    required: bool = True


@dataclass(frozen=True, slots=True)
class ModelToolDefinition:
    name: str
    description: str
    parameters: tuple[ModelToolParameter, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelToolArgument:
    name: str
    value: str


@dataclass(frozen=True, slots=True)
class ModelToolCall:
    call_id: str
    name: str
    arguments: tuple[ModelToolArgument, ...] = ()

    def argument_map(self) -> dict[str, str]:
        return {argument.name: argument.value for argument in self.arguments}


@dataclass(frozen=True, slots=True)
class ConversationMessage:
    role: MessageRole
    content: str = ""
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[ModelToolCall, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelRequest:
    messages: tuple[ConversationMessage, ...]
    tools: tuple[ModelToolDefinition, ...] = ()
    max_output_tokens: int | None = None
    temperature: float | None = None


@dataclass(frozen=True, slots=True)
class ModelResponse:
    content: str
    finish_reason: str
    tool_calls: tuple[ModelToolCall, ...] = ()
    profile_id: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class ModelInvocationError(RuntimeError):
    profile_id: str
    detail: str
    rate_limited: bool = False

    def __str__(self) -> str:
        return f"model route {self.profile_id!r} failed: {self.detail}"


class ModelProvider(Protocol):
    def complete(
        self, profile: ModelProfile, request: ModelRequest
    ) -> ModelResponse: ...


class ModelEventSink(Protocol):
    def __call__(self, event: Event) -> None: ...


class RoutedModelRuntime:
    def __init__(
        self,
        router: ModelRouter,
        provider: ModelProvider,
        event_sink: ModelEventSink | None = None,
    ) -> None:
        self.router = router
        self.provider = provider
        self.event_sink = event_sink

    def complete(
        self,
        role: Role,
        request: ModelRequest,
        trace_id: str | None = None,
    ) -> ModelResponse:
        attempted: list[str] = []
        last_failure: ModelInvocationError | None = None
        while True:
            try:
                profile = self.router.select(role, attempted)
            except NoRouteAvailable:
                if last_failure is not None:
                    raise last_failure
                raise
            try:
                return self._complete_once(profile, request, trace_id)
            except ModelInvocationError as exc:
                attempted.append(profile.id)
                last_failure = exc

    def _complete_once(
        self,
        profile: ModelProfile,
        request: ModelRequest,
        trace_id: str | None,
    ) -> ModelResponse:
        started = time.perf_counter()
        span_id = new_id("model_")
        ok = False
        rate_limited = False
        token_count = 0
        error_name: str | None = None
        self._emit(
            Event(
                EventType.MODEL_REQUEST_STARTED,
                Component.LLM,
                trace_id=trace_id,
                span_id=span_id,
                status=Status.RUNNING,
                summary="native model request started",
                metadata={
                    "profile_id": profile.id,
                    "provider": profile.provider,
                    "model": profile.model,
                    "message_count": len(request.messages),
                    "tool_count": len(request.tools),
                },
            )
        )
        try:
            raw_response = self.provider.complete(profile, request)
            latency_ms = (time.perf_counter() - started) * 1000
            response = replace(
                raw_response,
                profile_id=profile.id,
                model=raw_response.model or profile.model,
                latency_ms=latency_ms,
            )
            ok = True
            token_count = response.total_tokens
            self._emit(
                Event(
                    EventType.MODEL_REQUEST_COMPLETED,
                    Component.LLM,
                    trace_id=trace_id,
                    span_id=span_id,
                    duration_ms=latency_ms,
                    summary="native model request completed",
                    metrics={"tokens": float(token_count)},
                    metadata={
                        "profile_id": profile.id,
                        "model": response.model,
                        "finish_reason": response.finish_reason,
                        "tool_call_count": len(response.tool_calls),
                    },
                )
            )
            return response
        except ModelInvocationError as exc:
            error_name = type(exc).__name__
            rate_limited = exc.rate_limited
            raise
        finally:
            if not ok:
                active_exception = sys.exc_info()[0]
                if error_name is None and active_exception is not None:
                    error_name = active_exception.__name__
                self._emit(
                    Event(
                        EventType.MODEL_REQUEST_FAILED,
                        Component.LLM,
                        trace_id=trace_id,
                        span_id=span_id,
                        status=Status.FAILED,
                        duration_ms=(time.perf_counter() - started) * 1000,
                        summary="native model request failed",
                        metadata={
                            "profile_id": profile.id,
                            "model": profile.model,
                            "error_type": error_name,
                            "rate_limited": rate_limited,
                        },
                    )
                )
            self.router.release(
                profile.id,
                ok=ok,
                latency_ms=(time.perf_counter() - started) * 1000,
                rate_limited=rate_limited,
                tokens=token_count,
                error=error_name,
            )

    def _emit(self, event: Event) -> None:
        if self.event_sink is not None:
            self.event_sink(event)
