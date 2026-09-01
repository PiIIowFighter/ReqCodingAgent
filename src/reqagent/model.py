from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence


ROLES = frozenset({"system", "user", "assistant", "tool"})
FINISH_REASONS = frozenset({"tool_calls", "stop", "length", "refusal", "error"})


@dataclass(frozen=True)
class NormalizedToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]

    def __post_init__(self) -> None:
        if not self.call_id.strip() or not self.name.strip():
            raise ValueError("tool call id and name must be non-empty")
        if not isinstance(self.arguments, dict):
            raise ValueError("tool call arguments must be an object")


@dataclass(frozen=True)
class ModelMessage:
    role: str
    text: str = ""
    tool_calls: tuple[NormalizedToolCall, ...] = ()
    tool_results: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"unknown message role: {self.role}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "text": self.text,
            "tool_calls": [
                {"call_id": call.call_id, "name": call.name, "arguments": call.arguments}
                for call in self.tool_calls
            ],
            "tool_results": list(self.tool_results),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ModelMessage":
        if set(value) != {"role", "text", "tool_calls", "tool_results"}:
            raise ValueError("invalid model message fields")
        return cls(
            role=value["role"],
            text=value["text"],
            tool_calls=tuple(NormalizedToolCall(**call) for call in value["tool_calls"]),
            tool_results=tuple(value["tool_results"]),
        )


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]

    def __post_init__(self) -> None:
        if not self.name or self.input_schema.get("type") != "object":
            raise ValueError("tool definition requires a name and object schema")
        if self.input_schema.get("additionalProperties") is not False:
            raise ValueError("tool schema must reject additional properties")


@dataclass(frozen=True)
class ModelRequest:
    messages: tuple[ModelMessage, ...]
    tools: tuple[ToolDefinition, ...]
    max_output_tokens: int
    timeout_seconds: int


@dataclass(frozen=True)
class ModelResponse:
    text: str = ""
    tool_calls: tuple[NormalizedToolCall, ...] = ()
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str = "stop"
    provider_request_id: str | None = None
    actual_model: str | None = None

    def __post_init__(self) -> None:
        if self.finish_reason not in FINISH_REASONS:
            raise ValueError(f"unknown finish reason: {self.finish_reason}")
        if any(not isinstance(value, int) or value < 0 for value in self.usage.values()):
            raise ValueError("usage values must be non-negative integers")

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "tool_calls": [
                {"call_id": call.call_id, "name": call.name, "arguments": call.arguments}
                for call in self.tool_calls
            ],
            "usage": self.usage,
            "finish_reason": self.finish_reason,
            "provider_request_id": self.provider_request_id,
            "actual_model": self.actual_model,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ModelResponse":
        allowed = {"text", "tool_calls", "usage", "finish_reason", "provider_request_id", "actual_model"}
        if set(value) - allowed:
            raise ValueError("invalid model response fields")
        calls = []
        for raw in value.get("tool_calls", []):
            if not isinstance(raw, dict) or set(raw) != {"call_id", "name", "arguments"}:
                raise ValueError("invalid tool call fields")
            calls.append(NormalizedToolCall(**raw))
        return cls(
            text=value.get("text", ""),
            tool_calls=tuple(calls),
            usage=dict(value.get("usage", {})),
            finish_reason=value.get("finish_reason", "stop"),
            provider_request_id=value.get("provider_request_id"),
            actual_model=value.get("actual_model"),
        )


class ModelError(RuntimeError):
    def __init__(
        self,
        category: str,
        message: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
        provider_request_id: str | None = None,
    ):
        super().__init__(message)
        self.category = category
        self.retryable = retryable
        self.status_code = status_code if isinstance(status_code, int) else None
        self.provider_request_id = provider_request_id


class ModelAdapter(Protocol):
    def complete(self, request: ModelRequest) -> ModelResponse: ...


class ScriptedModel:
    def __init__(self, responses: Sequence[ModelResponse | dict[str, Any]], *, position: int = 0):
        self.responses = tuple(
            response if isinstance(response, ModelResponse) else ModelResponse.from_dict(response)
            for response in responses
        )
        self.position = position
        self.identity = {"provider": "scripted"}

    def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        if self.position >= len(self.responses):
            raise ModelError("script_exhausted", "scripted model has no response left")
        response = self.responses[self.position]
        self.position += 1
        return response
