from __future__ import annotations

from ..adaptive import AdaptiveRefinementState, brief_schema
from ..model import ToolDefinition
from .base import ToolEnvelope, object_schema


def brief_definition() -> ToolDefinition:
    return ToolDefinition(
        "record_requirement_brief",
        "Record a compact evidence-backed working hypothesis. The original task remains authoritative.",
        brief_schema(),
    )


def record_brief(state: AdaptiveRefinementState, args: dict) -> ToolEnvelope:
    passed, errors = state.record_brief(args)
    if not passed:
        return ToolEnvelope(False, "record_requirement_brief", {"errors": errors}, {"kind": "requirement_gate", "message": errors[0]}, False, {})
    return ToolEnvelope(True, "record_requirement_brief", {"gate": "passed", "brief_bytes": len(state.brief_message().encode()), "ranked_candidates": state.ranked_candidates}, None, False, {})


def reflection_definition() -> ToolDefinition:
    return ToolDefinition(
        "reflect_on_patch",
        "Before submit, accept the patch or request the single permitted evidence-driven brief revision.",
        object_schema({"decision": {"type": "string", "enum": ["accept", "revise"]}, "reason": {"type": "string", "minLength": 1, "maxLength": 500}}, ["decision", "reason"]),
    )


def reflect(state: AdaptiveRefinementState, args: dict) -> ToolEnvelope:
    passed, error = state.reflect(args["decision"], args["reason"])
    if not passed:
        return ToolEnvelope(False, "reflect_on_patch", {}, {"kind": "reflection_gate", "message": error}, False, {})
    return ToolEnvelope(True, "reflect_on_patch", {"decision": args["decision"], "revision_count": state.revision_count, "phase": state.phase}, None, False, {})
