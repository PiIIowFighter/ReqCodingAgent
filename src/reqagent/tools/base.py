from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from jsonschema import Draft202012Validator

from ..model import ToolDefinition


@dataclass(frozen=True)
class ToolEnvelope:
    ok: bool
    tool: str
    data: dict[str, Any]
    error: dict[str, str] | None
    truncated: bool
    meta: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "tool": self.tool, "data": self.data, "error": self.error, "truncated": self.truncated, "meta": self.meta}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


Handler = Callable[[dict[str, Any]], ToolEnvelope]


class ToolRegistry:
    def __init__(self, *, refinement=None) -> None:
        self._tools: dict[str, tuple[ToolDefinition, Handler]] = {}
        self.history: list[dict[str, Any]] = []
        self.adapter_identity: dict[str, Any] = {"provider": "scripted"}
        self.refinement = refinement

    def register(self, definition: ToolDefinition, handler: Handler) -> None:
        if definition.name in self._tools:
            raise ValueError(f"duplicate tool: {definition.name}")
        Draft202012Validator.check_schema(definition.input_schema)
        self._tools[definition.name] = (definition, handler)

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        adaptive = getattr(self, "adaptive", None)
        hidden = set()
        if adaptive is not None:
            if adaptive.phase != "refining":
                hidden.add("record_requirement_brief")
            if adaptive.route.mode == "fast" or adaptive.phase in {"refining", "accepted"} or not adaptive.requires_reflection:
                hidden.add("reflect_on_patch")
        return tuple(value[0] for name, value in self._tools.items() if name not in hidden)

    @property
    def schema_definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(value[0] for value in self._tools.values())

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolEnvelope:
        started = time.monotonic()
        blocked = {"apply_patch", "run_command", "submit"}
        adaptive = getattr(self, "adaptive", None)
        if adaptive is not None and adaptive.route.mode == "refine" and adaptive.phase == "refining" and name in blocked:
            result = ToolEnvelope(
                False,
                name,
                {},
                {"kind": "requirement_gate", "message": "record a compact evidence-backed RequirementBrief before mutation or execution"},
                False,
                {},
            )
        elif adaptive is not None and adaptive.route.mode == "refine" and adaptive.requires_reflection and name == "submit" and adaptive.phase != "accepted":
            result = ToolEnvelope(
                False,
                name,
                {},
                {"kind": "reflection_gate", "message": "reflect_on_patch must accept the patch before submit"},
                False,
                {},
            )
        elif self.refinement is not None and not self.refinement.approved and name in blocked:
            result = ToolEnvelope(
                False,
                name,
                {},
                {"kind": "requirement_gate", "message": "record a valid RequirementBaseline before mutation, execution, or submit"},
                False,
                {},
            )
        elif name not in self._tools:
            result = ToolEnvelope(False, name, {}, {"kind": "unknown_tool", "message": "unknown tool"}, False, {})
        else:
            definition, handler = self._tools[name]
            errors = sorted(Draft202012Validator(definition.input_schema).iter_errors(arguments), key=lambda item: list(item.path))
            if errors:
                result = ToolEnvelope(False, name, {}, {"kind": "invalid_arguments", "message": errors[0].message}, False, {})
            else:
                try:
                    result = handler(arguments)
                except (OSError, ValueError) as exc:
                    result = ToolEnvelope(False, name, {}, {"kind": "tool_error", "message": str(exc)}, False, {})
        elapsed = int((time.monotonic() - started) * 1000)
        result = ToolEnvelope(result.ok, result.tool, result.data, result.error, result.truncated, {**result.meta, "duration_ms": elapsed})
        self.history.append({"name": name, "arguments": arguments, "result": result.to_dict()})
        return result


def object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}
