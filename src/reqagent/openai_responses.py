from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any

from jsonschema import Draft202012Validator

from .config import AgentConfig
from .live import _redacted_endpoint, _tool_result_content
from .model import ModelError, ModelMessage, ModelRequest, ModelResponse, NormalizedToolCall


_SENSITIVE = re.compile(
    r"(?i)(?:[a-z]:[\\/]|/)(?:[^\s]+[\\/])+[^\s]+|"
    r"\b(api[_-]?key|auth(?:entication)?[_-]?token|password|secret|authorization)\b\s*[:=]\s*\S+|"
    r"encrypted_content|reasoning"
)


def _sanitize_error(message: str) -> str:
    cleaned = _SENSITIVE.sub("[redacted]", message)
    return cleaned if len(cleaned) <= 240 else cleaned[:239] + "…"


def _safe_request_id(value: Any) -> str | None:
    text = value if isinstance(value, str) else None
    return text if text and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", text) else None


def _item_field(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _item_to_wire(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return dict(item)
    if hasattr(item, "model_dump"):
        return item.model_dump(exclude_none=True)
    return {key: getattr(item, key) for key in dir(item) if not key.startswith("_") and not callable(getattr(item, key))}


class OpenAIResponsesAdapter:
    def __init__(self, config: AgentConfig, *, client: Any | None = None):
        model = config.model
        if model["provider"] != "local_reverse_proxy" or model["protocol"] != "openai_responses":
            raise ValueError("unsupported live provider or protocol")
        base_url = os.environ[model["base_url_env"]]
        api_key = os.environ[model["api_key_env"]]
        if client is None:
            from openai import OpenAI

            client = OpenAI(
                base_url=base_url,
                api_key=api_key,
                timeout=float(config.budgets["model_timeout_seconds"]),
                max_retries=0,
            )
        self.client = client
        self.config = config
        self.actual_model: str | None = None
        self.identity = {
            "provider": model["provider"],
            "protocol": model["protocol"],
            "model": model["model"],
            "base_url_env": model["base_url_env"],
            "api_key_env": model["api_key_env"],
            "endpoint": _redacted_endpoint(base_url),
            "endpoint_sha256": hashlib.sha256(base_url.encode("utf-8")).hexdigest(),
        }
        self._instructions: str | None = None
        self._wire_input: list[Any] = []
        self._initial_user_added = False
        self._processed_call_ids: set[str] = set()

    def reset_wire_history(self) -> None:
        """Start a new explicit Responses conversation without changing adapter identity."""
        self._instructions = None
        self._wire_input = []
        self._initial_user_added = False
        self._processed_call_ids = set()

    def _sync_wire_input(self, request: ModelRequest) -> None:
        system_parts = [message.text for message in request.messages if message.role == "system" and message.text]
        self._instructions = "\n".join(system_parts)
        for message in request.messages:
            if message.role == "user" and not self._initial_user_added:
                self._wire_input.append({"role": "user", "content": message.text})
                self._initial_user_added = True
            elif message.role == "tool":
                for result in message.tool_results:
                    call_id = str(result.get("call_id", "")).strip()
                    if not call_id or call_id in self._processed_call_ids:
                        continue
                    self._wire_input.append({
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps(_tool_result_content(result), ensure_ascii=False, sort_keys=True),
                    })
                    self._processed_call_ids.add(call_id)

    def _build_tools(self, request: ModelRequest) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            }
            for tool in request.tools
        ]

    def _raise_model_error(self, exc: Exception) -> None:
        status = getattr(exc, "status_code", None)
        details = {
            "status_code": status if isinstance(status, int) else None,
            "provider_request_id": _safe_request_id(getattr(exc, "request_id", None)),
        }
        if isinstance(exc, TimeoutError):
            raise ModelError("timeout", "model request timed out", retryable=True, **details) from exc
        if isinstance(exc, ConnectionError):
            raise ModelError("connection", "model connection failed", retryable=True, **details) from exc
        error_name = type(exc).__name__
        if error_name == "APITimeoutError":
            raise ModelError("timeout", "model request timed out", retryable=True, **details) from exc
        if error_name == "APIConnectionError":
            raise ModelError("connection", "model connection failed", retryable=True, **details) from exc
        if status == 429 or error_name == "RateLimitError":
            raise ModelError("rate_limit", "model rate limit exceeded", retryable=True, **details) from exc
        if isinstance(status, int) and status >= 500:
            raise ModelError("server", "model server error", retryable=True, **details) from exc
        if status in {401, 403} or error_name == "AuthenticationError":
            raise ModelError("authentication", "model authentication failed", **details) from exc
        if isinstance(status, int) and status == 404:
            raise ModelError("request", "model endpoint or resource was not found", **details) from exc
        if isinstance(status, int) and 400 <= status < 500:
            raise ModelError("request", "model request was rejected", **details) from exc
        raise ModelError("protocol", _sanitize_error("model adapter failed"), **details) from exc

    def _parse_output(self, response: Any, schemas: dict[str, dict[str, Any]]) -> ModelResponse:
        calls: list[NormalizedToolCall] = []
        text_parts: list[str] = []
        refusal = False
        for item in _item_field(response, "output", []) or []:
            item_type = _item_field(item, "type")
            if item_type == "message":
                for block in _item_field(item, "content", []) or []:
                    block_type = _item_field(block, "type")
                    if block_type in {"output_text", "text"}:
                        text_parts.append(str(_item_field(block, "text", "")))
                    elif block_type == "refusal":
                        refusal = True
            elif item_type == "function_call":
                name = _item_field(item, "name")
                arguments_raw = _item_field(item, "arguments", "{}")
                if name not in schemas:
                    raise ValueError("unknown tool")
                if not isinstance(arguments_raw, str):
                    raise ValueError("arguments must be a JSON string")
                try:
                    arguments = json.loads(arguments_raw or "{}")
                except json.JSONDecodeError as exc:
                    raise ValueError("arguments must decode as JSON") from exc
                if not isinstance(arguments, dict):
                    raise ValueError("arguments must be an object")
                errors = list(Draft202012Validator(schemas[name]).iter_errors(arguments))
                if errors:
                    raise ValueError(errors[0].message)
                calls.append(NormalizedToolCall(str(_item_field(item, "call_id")), name, arguments))
            elif item_type == "reasoning":
                continue
            elif item_type not in {None, "reasoning"}:
                raise ValueError(f"unknown output item: {item_type}")
        status = _item_field(response, "status", "completed")
        incomplete = _item_field(response, "incomplete_details")
        if refusal:
            finish_reason = "refusal"
        elif calls:
            finish_reason = "tool_calls"
        elif status == "incomplete" or (isinstance(incomplete, dict) and incomplete.get("reason") == "max_output_tokens"):
            finish_reason = "length"
        elif status == "failed":
            finish_reason = "error"
        else:
            finish_reason = "stop"
        usage_obj = _item_field(response, "usage")
        usage = {
            name: value
            for name in ("input_tokens", "output_tokens", "total_tokens")
            if isinstance((value := _item_field(usage_obj, name)), int) and value >= 0
        }
        actual_model = _item_field(response, "model")
        self.actual_model = actual_model
        return ModelResponse(
            text="".join(text_parts),
            tool_calls=tuple(calls),
            usage=usage,
            finish_reason=finish_reason,
            provider_request_id=_item_field(response, "id"),
            actual_model=actual_model,
        )

    def complete(self, request: ModelRequest) -> ModelResponse:
        schemas = {tool.name: tool.input_schema for tool in request.tools}
        self._sync_wire_input(request)
        payload: dict[str, Any] = {
            "model": self.config.model["model"],
            "input": list(self._wire_input),
            "instructions": self._instructions or "",
            "tools": self._build_tools(request),
            "tool_choice": "required",
            "max_output_tokens": request.max_output_tokens,
            "store": False,
        }
        for name in ("temperature", "seed"):
            value = self.config.model[name]
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                payload[name] = value
        try:
            response = self.client.responses.create(**payload)
        except ModelError:
            raise
        except Exception as exc:
            self._raise_model_error(exc)
        try:
            normalized = self._parse_output(response, schemas)
            for item in _item_field(response, "output", []) or []:
                self._wire_input.append(_item_to_wire(item))
            return normalized
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ModelError("malformed_response", "malformed model response") from exc
