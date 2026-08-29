from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .trace import atomic_json, atomic_text


SCHEMA_VERSION = 1


def canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


REQUIRED_RESUME_FIELDS = frozenset({
    "run_id", "source", "base_commit", "code_hash", "config_hash",
    "system_prompt_hash", "protocol_prompt_hash", "tool_schema_hash", "task_hash",
    "diff_hash", "protected_fingerprint", "budgets", "next_state", "elapsed_seconds",
    "steps", "tool_calls", "invalid_outputs", "usage", "adapter_position",
    "repeat_fingerprint", "repeat_count", "warnings", "messages", "context_summary",
    "tool_history", "pending_tool_calls", "next_tool_index", "adapter_identity_hash",
})
_ALLOWED_NEXT_STATES = frozenset({"call_model", "execute"})
_IDENTITY_LABELS = {
    "run_id": "run id", "source": "source", "base_commit": "base commit",
    "code_hash": "code", "config_hash": "config", "system_prompt_hash": "system prompt",
    "protocol_prompt_hash": "protocol prompt", "tool_schema_hash": "tool schema",
    "task_hash": "task", "diff_hash": "workspace", "protected_fingerprint": "protected fingerprint",
    "adapter_identity_hash": "adapter identity",
}


def validate_resume_payload(payload: dict[str, Any], expected: dict[str, Any], budgets: dict[str, Any]) -> dict[str, Any]:
    missing = REQUIRED_RESUME_FIELDS - set(payload)
    if missing:
        raise ValueError(f"resume checkpoint is incomplete: {sorted(missing)}")
    if payload["next_state"] not in _ALLOWED_NEXT_STATES:
        raise ValueError("resume refused: illegal next state")
    pending = payload["pending_tool_calls"]
    index = payload["next_tool_index"]
    if not isinstance(pending, list) or not isinstance(index, int) or isinstance(index, bool) or index < 0:
        raise ValueError("resume refused: invalid pending tool state")
    for call in pending:
        if not isinstance(call, dict) or set(call) != {"call_id", "name", "arguments"} or not isinstance(call["arguments"], dict):
            raise ValueError("resume refused: invalid pending tool call")
    if payload["next_state"] == "execute":
        if not pending or index >= len(pending):
            raise ValueError("resume refused: execute state has no pending tool call")
        assistant_calls = next(
            (message.get("tool_calls", []) for message in reversed(payload["messages"]) if message.get("role") == "assistant"),
            [],
        )
        if assistant_calls != pending:
            raise ValueError("resume refused: pending tool calls do not match assistant response")
    elif pending or index != 0:
        raise ValueError("resume refused: call_model state contains pending tool calls")
    for key, label in _IDENTITY_LABELS.items():
        if payload[key] != expected[key]:
            raise ValueError(f"resume refused: {label} changed")
    original = payload["budgets"]
    if set(original) != set(budgets):
        raise ValueError("resume refused: budget keys changed")
    for key, value in budgets.items():
        old = original[key]
        if isinstance(old, (int, float)) and isinstance(value, (int, float)) and value > old:
            raise ValueError("resume refused: budget was loosened")
        if not isinstance(old, (int, float)) and value != old:
            raise ValueError("resume refused: budget changed")
    return payload


class CheckpointStore:
    def __init__(self, run_path: Path):
        self.run_path = run_path
        self.root = run_path / "checkpoints"
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, sequence: int, payload: dict[str, Any]) -> Path:
        body = {"schema_version": SCHEMA_VERSION, "payload": payload}
        body["checksum"] = canonical_hash(body)
        target = self.root / f"{sequence:06d}.json"
        atomic_json(target, body)
        loaded = json.loads(target.read_text(encoding="utf-8"))
        self._verify(loaded)
        atomic_text(self.run_path / "LATEST", target.name + "\n")
        return target

    def load(self) -> dict[str, Any]:
        if (self.run_path / "COMPLETE").exists():
            raise ValueError("completed runs cannot be resumed")
        latest = (self.run_path / "LATEST").read_text(encoding="utf-8").strip()
        value = json.loads((self.root / latest).read_text(encoding="utf-8"))
        self._verify(value)
        return value["payload"]

    @staticmethod
    def _verify(value: dict[str, Any]) -> None:
        if set(value) != {"schema_version", "payload", "checksum"} or value["schema_version"] != SCHEMA_VERSION:
            raise ValueError("invalid checkpoint schema")
        expected = value["checksum"]
        body = {"schema_version": value["schema_version"], "payload": value["payload"]}
        if canonical_hash(body) != expected:
            raise ValueError("checkpoint checksum mismatch")
