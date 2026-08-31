from __future__ import annotations

from ..model import ToolDefinition
from ..requirements import RequirementRefinementState, requirement_baseline_schema
from .base import ToolEnvelope


def definition() -> ToolDefinition:
    return ToolDefinition(
        "record_requirement_baseline",
        "Record the evidence-backed RequirementBaseline. Mutation, commands, and submit remain blocked until this gate passes.",
        requirement_baseline_schema(),
    )


def record_requirement_baseline(state: RequirementRefinementState, args: dict) -> ToolEnvelope:
    passed, errors = state.record(args)
    if not passed:
        return ToolEnvelope(
            False,
            "record_requirement_baseline",
            {"gate": "rejected", "errors": errors},
            {"kind": "requirement_gate", "message": errors[0]},
            False,
            {},
        )
    return ToolEnvelope(
        True,
        "record_requirement_baseline",
        {
            "gate": "passed",
            "selected_skills": list(state.selected_skills),
            "refined_summary": state.baseline["refined_summary"],
        },
        None,
        False,
        {},
    )
