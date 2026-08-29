from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from jsonschema import Draft202012Validator

from .config import AgentConfig
from .model import ModelError, ModelMessage, ModelRequest, ModelResponse, NormalizedToolCall
from .tools.command import ContainerCommandExecutor


_STOP_REASONS = {
    "tool_use": "tool_calls",
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "refusal": "refusal",
}


def _redacted_endpoint(value: str) -> str:
    parts = urlsplit(value)
    host = parts.hostname or ""
    if parts.port is not None:
        host += f":{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def _tool_result_content(result: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if key != "call_id"}


class AnthropicMessagesAdapter:
    def __init__(self, config: AgentConfig, *, client: Any | None = None):
        model = config.model
        if model["provider"] != "local_reverse_proxy" or model["protocol"] != "anthropic_messages":
            raise ValueError("unsupported live provider or protocol")
        base_url = os.environ[model["base_url_env"]]
        auth_token = os.environ[model["api_key_env"]]
        if client is None:
            import anthropic
            client = anthropic.Anthropic(
                base_url=base_url,
                auth_token=auth_token,
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

    def complete(self, request: ModelRequest) -> ModelResponse:
        system_parts: list[str] = []
        messages: list[dict[str, Any]] = []
        schemas = {tool.name: tool.input_schema for tool in request.tools}
        for message in request.messages:
            if message.role == "system":
                system_parts.append(message.text)
            elif message.role == "user":
                messages.append({"role": "user", "content": message.text})
            elif message.role == "assistant":
                content: list[dict[str, Any]] = []
                if message.text:
                    content.append({"type": "text", "text": message.text})
                content.extend(
                    {"type": "tool_use", "id": call.call_id, "name": call.name, "input": call.arguments}
                    for call in message.tool_calls
                )
                messages.append({"role": "assistant", "content": content})
            else:
                content = []
                for result in message.tool_results:
                    content.append({
                        "type": "tool_result",
                        "tool_use_id": result["call_id"],
                        "content": json.dumps(_tool_result_content(result), ensure_ascii=False, sort_keys=True),
                        "is_error": not result.get("ok", False),
                    })
                messages.append({"role": "user", "content": content})
        payload: dict[str, Any] = {
            "model": self.config.model["model"],
            "max_tokens": request.max_output_tokens,
            "system": "\n".join(system_parts),
            "messages": messages,
            "tools": [
                {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}
                for tool in request.tools
            ],
        }
        for name in ("temperature", "seed"):
            value = self.config.model[name]
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                payload[name] = value
        try:
            response = self.client.messages.create(**payload)
        except TimeoutError as exc:
            raise ModelError("timeout", "model request timed out", retryable=True) from exc
        except ConnectionError as exc:
            raise ModelError("connection", "model connection failed", retryable=True) from exc
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            error_name = type(exc).__name__
            if error_name == "APITimeoutError":
                raise ModelError("timeout", "model request timed out", retryable=True) from exc
            if error_name == "APIConnectionError":
                raise ModelError("connection", "model connection failed", retryable=True) from exc
            if status == 429:
                raise ModelError("rate_limit", "model rate limit exceeded", retryable=True) from exc
            if isinstance(status, int) and status >= 500:
                raise ModelError("server", "model server error", retryable=True) from exc
            if status in {401, 403}:
                raise ModelError("authentication", "model authentication failed") from exc
            if isinstance(status, int) and 400 <= status < 500:
                raise ModelError("request", "model request was rejected") from exc
            raise ModelError("protocol", "model adapter failed") from exc
        try:
            calls: list[NormalizedToolCall] = []
            text: list[str] = []
            for block in response.content:
                if block.type == "text":
                    text.append(block.text)
                elif block.type == "tool_use":
                    if block.name not in schemas or not isinstance(block.input, dict):
                        raise ValueError("unknown tool or non-object arguments")
                    errors = list(Draft202012Validator(schemas[block.name]).iter_errors(block.input))
                    if errors:
                        raise ValueError(errors[0].message)
                    calls.append(NormalizedToolCall(block.id, block.name, block.input))
                elif block.type not in {"thinking", "redacted_thinking"}:
                    raise ValueError(f"unknown content block: {block.type}")
            finish_reason = _STOP_REASONS[response.stop_reason]
            usage = {
                name: value for name in (
                    "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"
                ) if isinstance((value := getattr(response.usage, name, None)), int) and value >= 0
            }
            self.actual_model = getattr(response, "model", None)
            return ModelResponse(
                text="".join(text), tool_calls=tuple(calls), usage=usage,
                finish_reason=finish_reason,
                provider_request_id=getattr(response, "_request_id", None),
                actual_model=self.actual_model,
            )
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ModelError("malformed_response", "malformed model response") from exc


def _docker_prefix() -> tuple[str, ...]:
    return ("wsl.exe", "--", "docker") if sys.platform == "win32" else ("docker",)


def _wsl_path(path: Path) -> str:
    completed = subprocess.run(
        ["wsl.exe", "--", "wslpath", "-a", path.as_posix()],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    if completed.returncode:
        raise ValueError("cannot translate workspace path for WSL2 Docker")
    return completed.stdout.strip()


def build_live_runtime(config: AgentConfig, *, run_id: str, client: Any | None = None, inspect_image: bool = True):
    config.validate(live=True)
    image = config.workspace["container_image"]
    prefix = _docker_prefix()
    if inspect_image:
        inspected = subprocess.run(
            [*prefix, "image", "inspect", image],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
        if inspected.returncode:
            raise ValueError("configured container image is not available locally")
    converter = _wsl_path if sys.platform == "win32" else None
    return (
        AnthropicMessagesAdapter(config, client=client),
        ContainerCommandExecutor(command_prefix=prefix, image=image, run_id=run_id, path_converter=converter),
    )
