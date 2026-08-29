from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


_PLACEHOLDER = "__FILL_"
_TOP_KEYS = {"mode", "model", "budgets", "workspace", "script"}
_MODEL_KEYS = {
    "provider", "base_url_env", "api_key_env", "model", "protocol",
    "native_tool_calling", "context_window_tokens", "max_output_tokens",
    "temperature", "seed",
}
_BUDGET_KEYS = {
    "max_steps", "max_tool_calls", "wall_clock_seconds", "model_timeout_seconds",
    "command_timeout_seconds", "max_invalid_outputs", "max_retries",
    "context_trigger_ratio", "keep_recent_rounds",
}
_WORKSPACE_KEYS = {"max_patch_files", "max_patch_lines", "max_patch_bytes", "protected_paths"}


def _exact(value: dict[str, Any], expected: set[str], where: str) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        unknown = sorted(set(value) - expected)
        raise ValueError(f"invalid {where} keys: missing={missing}, unknown={unknown}")


def _redact_url(value: str) -> str:
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


@dataclass(frozen=True)
class AgentConfig:
    raw: dict[str, Any]
    source: Path

    @classmethod
    def load(cls, path: str | Path) -> "AgentConfig":
        source = Path(path).resolve()
        try:
            value = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot load config: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError("config root must be an object")
        config = cls(value, source)
        config.validate()
        return config

    @property
    def mode(self) -> str:
        return self.raw["mode"]

    @property
    def budgets(self) -> dict[str, Any]:
        return self.raw["budgets"]

    @property
    def workspace(self) -> dict[str, Any]:
        return self.raw["workspace"]

    @property
    def model(self) -> dict[str, Any]:
        return self.raw["model"]

    @property
    def script(self) -> list[dict[str, Any]]:
        return self.raw["script"]

    def validate(self, *, live: bool = False) -> None:
        _exact(self.raw, _TOP_KEYS, "config")
        _exact(self.model, _MODEL_KEYS, "model")
        _exact(self.budgets, _BUDGET_KEYS, "budgets")
        _exact(self.workspace, _WORKSPACE_KEYS, "workspace")
        if self.mode not in {"scripted", "live"}:
            raise ValueError("mode must be scripted or live")
        integer_fields = [key for key in _BUDGET_KEYS if key not in {"context_trigger_ratio"}]
        for key in integer_fields:
            value = self.budgets[key]
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"budget {key} must be a positive integer")
        ratio = self.budgets["context_trigger_ratio"]
        if not isinstance(ratio, (int, float)) or isinstance(ratio, bool) or not 0 < ratio <= 1:
            raise ValueError("context_trigger_ratio must be in (0, 1]")
        for key in ("max_patch_files", "max_patch_lines", "max_patch_bytes"):
            if not isinstance(self.workspace[key], int) or self.workspace[key] <= 0:
                raise ValueError(f"workspace {key} must be a positive integer")
        if not isinstance(self.workspace["protected_paths"], list) or not all(
            isinstance(item, str) and item for item in self.workspace["protected_paths"]
        ):
            raise ValueError("protected_paths must be a list of non-empty strings")
        if not isinstance(self.script, list):
            raise ValueError("script must be a list")
        if self.mode == "scripted" and not self.script:
            raise ValueError("scripted mode requires responses")
        for response in self.script:
            from .model import ModelResponse
            ModelResponse.from_dict(response)
        if live:
            missing = []
            for key, value in self.model.items():
                if isinstance(value, str) and (not value or value.startswith(_PLACEHOLDER)):
                    missing.append(key)
            for key in ("base_url_env", "api_key_env"):
                env_name = self.model[key]
                if isinstance(env_name, str) and not env_name.startswith(_PLACEHOLDER) and not os.environ.get(env_name):
                    missing.append(f"env:{env_name}")
            if missing:
                raise ValueError("live configuration is incomplete: " + ", ".join(sorted(set(missing))))
            raise ValueError("live model adapter is not implemented or confirmed")

    def public_dict(self) -> dict[str, Any]:
        value = json.loads(json.dumps(self.raw))
        for key in ("base_url_env", "api_key_env"):
            env_name = value["model"].get(key)
            if isinstance(env_name, str) and os.environ.get(env_name):
                secret = os.environ[env_name]
                value["model"][key] = {"env": env_name, "configured": True}
                if key == "base_url_env":
                    value["model"][key]["value"] = _redact_url(secret)
        return value

    def canonical_hash(self) -> str:
        payload = json.dumps(self.raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
