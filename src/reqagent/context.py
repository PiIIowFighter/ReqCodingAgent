from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .model import ModelMessage


@dataclass(frozen=True)
class ContextSummary:
    inspected_files: tuple[str, ...] = ()
    findings: tuple[str, ...] = ()
    modifications: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    open_issues: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()
    no_progress_actions: tuple[str, ...] = ()
    diff_fingerprint: str = ""

    def text(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False, sort_keys=True)

    def to_dict(self) -> dict[str, Any]:
        return {key: list(value) if isinstance(value, tuple) else value for key, value in self.__dict__.items()}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ContextSummary":
        return cls(
            inspected_files=tuple(value.get("inspected_files", ())),
            findings=tuple(value.get("findings", ())),
            modifications=tuple(value.get("modifications", ())),
            commands=tuple(value.get("commands", ())),
            open_issues=tuple(value.get("open_issues", ())),
            next_actions=tuple(value.get("next_actions", ())),
            no_progress_actions=tuple(value.get("no_progress_actions", ())),
            diff_fingerprint=value.get("diff_fingerprint", ""),
        )


class ContextLedger:
    def __init__(self, system: str, task: str, *, context_window: int, trigger_ratio: float, keep_recent_rounds: int):
        self.system = system
        self.task = task
        self.context_window = context_window
        self.trigger_ratio = trigger_ratio
        self.keep_recent_rounds = keep_recent_rounds
        self.messages = [ModelMessage("system", system), ModelMessage("user", task)]
        self.summary = ContextSummary()

    def add(self, message: ModelMessage) -> None:
        self.messages.append(message)

    def estimate_tokens(self) -> int:
        return max(1, len(json.dumps([message.to_dict() for message in self.messages], ensure_ascii=False)) // 4)

    def compact_if_needed(self, tool_history: list[dict[str, Any]], diff_fingerprint: str) -> bool:
        if self.estimate_tokens() < int(self.context_window * self.trigger_ratio):
            return False
        preserve = 2 + self.keep_recent_rounds * 2
        if len(self.messages) <= preserve:
            return False
        old = self.messages[2:-self.keep_recent_rounds * 2]
        inspected = sorted({item["arguments"].get("path") for item in tool_history if item["name"] in {"read_file", "list_files"} and item["arguments"].get("path")})
        modifications = sorted({path for item in tool_history if item["name"] == "apply_patch" and item["result"]["ok"] for path in item["result"]["data"].get("paths", [])})
        commands = tuple(item["arguments"]["command"] for item in tool_history if item["name"] == "run_command")
        self.summary = ContextSummary(tuple(inspected), tuple(message.text[:500] for message in old if message.text), tuple(modifications), commands, (), (), (), diff_fingerprint)
        recent = self.messages[-self.keep_recent_rounds * 2:]
        self.messages = self.messages[:2] + [ModelMessage("system", "Earlier interaction summary: " + self.summary.text())] + recent
        return True

    def fingerprint(self) -> str:
        payload = json.dumps([message.to_dict() for message in self.messages], ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
