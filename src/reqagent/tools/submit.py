from __future__ import annotations

from typing import Any

from ..model import ToolDefinition
from .base import ToolEnvelope, object_schema


def definition() -> ToolDefinition:
    return ToolDefinition("submit", "Finish the task with a concise change summary and only tests actually run.", object_schema({"summary": {"type": "string"}, "tests": {"type": "array", "items": {"type": "string"}}, "limitations": {"type": "string"}}, ["summary", "tests"]))


def submit(command_history: list[dict[str, Any]], args: dict[str, Any]) -> ToolEnvelope:
    commands = [item["arguments"]["command"] for item in command_history if item["name"] == "run_command"]
    unverified = [test for test in args["tests"] if not any(test in command or command in test for command in commands)]
    return ToolEnvelope(True, "submit", {"summary": args["summary"], "tests": args["tests"], "limitations": args.get("limitations", ""), "unverified_test_claims": unverified}, None, False, {})
