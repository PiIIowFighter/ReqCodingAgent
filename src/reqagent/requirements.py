from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


ONTOLOGY_VERSION = "coding-requirement-ontology-v1"
ONTOLOGY = {
    "Change Intent": ("goal", "current_behavior_or_symptom", "expected_behavior"),
    "Code Scope": ("target_component", "relevant_symbol_or_api", "affected_consumers"),
    "Constraints": ("compatibility", "boundary_and_error_semantics", "excluded_scope"),
    "Validation": ("acceptance_criteria", "relevant_tests_or_checks"),
}
SLOT_NAMES = tuple(slot for slots in ONTOLOGY.values() for slot in slots)
SLOT_STATUSES = frozenset({"explicit", "inferred", "defaulted", "unresolved"})
AMBIGUITY_TYPES = frozenset({"omission", "referential_ambiguity", "specificity_reduction"})

SKILL_CATALOG = (
    {
        "id": "omission_recovery",
        "intent": "Recover missing goals, constraints, and acceptance evidence from the repository.",
        "use_when": "The task omits behavior, scope, constraints, or validation needed for a safe change.",
        "avoid_when": "The prompt already states the relevant behavior and checks, or evidence conflicts.",
        "steps": [
            "Locate the closest implementation and public API.",
            "Inspect focused tests, documentation, callers, and adjacent implementations.",
            "Record only repository-supported missing slots and preserve unresolved uncertainty.",
        ],
        "stop_condition": "Required gate slots are supported, or further focused search adds no evidence.",
        "enabled": True,
    },
    {
        "id": "reference_resolution",
        "intent": "Map vague references to the best-supported component, symbol, or behavior.",
        "use_when": "The task uses pronouns or phrases such as this feature or related handling.",
        "avoid_when": "The referent is explicit, or multiple candidates remain equally supported.",
        "steps": [
            "Extract each ambiguous reference and its surrounding behavior words.",
            "Search matching symbols, callers, tests, and documentation.",
            "Choose the narrowest strongly supported referent and record alternatives as unresolved.",
        ],
        "stop_condition": "One referent has repository evidence, or ambiguity cannot be conservatively resolved.",
        "enabled": True,
    },
    {
        "id": "specificity_expansion",
        "intent": "Translate low-specificity requests into observable behavior, boundaries, and checks.",
        "use_when": "The task asks to fix, improve, support, or handle something correctly without observable detail.",
        "avoid_when": "Expansion would invent behavior not supported by the task or repository.",
        "steps": [
            "Identify the current observable behavior and closest contract.",
            "Derive the smallest compatible expected behavior and boundary semantics.",
            "Name an executable focused check or an explicit observable acceptance criterion.",
        ],
        "stop_condition": "Expected behavior and acceptance criteria are executable or directly observable.",
        "enabled": True,
    },
)
SKILLS_BY_ID = {skill["id"]: skill for skill in SKILL_CATALOG}

_FORBIDDEN = re.compile(
    r"(?i)(hidden evaluator|gold patch|oracle|benchmark/private|api[_-]?key|auth(?:entication)?[_-]?token|password|secret)"
)
_WINDOWS_ABSOLUTE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]")


def _text_is_safe(value: str) -> bool:
    return not _FORBIDDEN.search(value) and not _WINDOWS_ABSOLUTE.search(value) and not value.startswith(("/", "\\\\"))


def _evidence_reference_is_safe(value: str) -> bool:
    if value == "task":
        return True
    path = value.rsplit(":", 1)[0] if re.search(r":\d+$", value) else value
    return bool(path) and _text_is_safe(path) and ".." not in path.replace("\\", "/").split("/")


def requirement_baseline_schema() -> dict[str, Any]:
    evidence = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["user_task", "code", "test", "documentation", "call_site", "api"]},
            "reference": {"type": "string", "minLength": 1},
            "detail": {"type": "string", "minLength": 1},
        },
        "required": ["kind", "reference", "detail"],
        "additionalProperties": False,
    }
    slot = {
        "type": "object",
        "properties": {
            "value": {"type": "string"},
            "status": {"type": "string", "enum": sorted(SLOT_STATUSES)},
            "evidence": {"type": "array", "items": evidence},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["value", "status", "evidence", "confidence"],
        "additionalProperties": False,
    }
    assumption = {
        "type": "object",
        "properties": {
            "value": {"type": "string", "minLength": 1},
            "provenance": {"type": "string", "minLength": 1},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["value", "provenance", "confidence"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "ambiguity_types": {"type": "array", "items": {"type": "string", "enum": sorted(AMBIGUITY_TYPES)}, "uniqueItems": True},
            "selected_skills": {"type": "array", "items": {"type": "string", "enum": sorted(SKILLS_BY_ID)}, "uniqueItems": True, "maxItems": 2},
            "slots": {
                "type": "object",
                "properties": {name: slot for name in SLOT_NAMES},
                "required": list(SLOT_NAMES),
                "additionalProperties": False,
            },
            "assumptions": {"type": "array", "items": assumption},
            "original_summary": {"type": "string", "minLength": 1},
            "refined_summary": {"type": "string", "minLength": 1},
        },
        "required": ["ambiguity_types", "selected_skills", "slots", "assumptions", "original_summary", "refined_summary"],
        "additionalProperties": False,
    }


def _validation_errors(value: dict[str, Any]) -> list[str]:
    slots = value["slots"]
    errors: list[str] = []
    if not slots["goal"]["value"].strip():
        errors.append("goal must be non-empty")
    if not slots["expected_behavior"]["value"].strip() and not slots["acceptance_criteria"]["value"].strip():
        errors.append("expected_behavior or acceptance_criteria must be non-empty")
    target = slots["target_component"]
    if not target["value"].strip():
        errors.append("target_component must be non-empty")
    elif target["status"] not in {"inferred", "defaulted", "explicit"} or not target["evidence"]:
        errors.append("target_component requires explicit or conservative repository-supported provenance")
    for name, slot in slots.items():
        text = slot["value"]
        if slot["status"] == "unresolved" and text.strip():
            errors.append(f"{name} unresolved slot must not claim a value")
        if slot["status"] != "unresolved" and text.strip() and not slot["evidence"]:
            errors.append(f"{name} requires evidence")
        if slot["status"] == "explicit" and slot["evidence"] and not any(item["kind"] == "user_task" for item in slot["evidence"]):
            errors.append(f"{name} explicit status requires user_task evidence")
        for item in slot["evidence"]:
            if not _evidence_reference_is_safe(item["reference"]):
                errors.append(f"{name} contains unsafe evidence reference")
    for skill_id in value["selected_skills"]:
        if not SKILLS_BY_ID[skill_id]["enabled"]:
            errors.append(f"selected skill is disabled: {skill_id}")
    if not value["ambiguity_types"] and value["selected_skills"]:
        errors.append("fast-path baseline cannot select ambiguity skills")
    serialized = json.dumps(value, ensure_ascii=False)
    if not _text_is_safe(serialized):
        errors.append("baseline contains forbidden or repository-external information")
    return sorted(set(errors))


@dataclass
class RequirementRefinementState:
    task: str
    approved: bool = False
    baseline: dict[str, Any] | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    context_injected: bool = False

    @property
    def selected_skills(self) -> tuple[str, ...]:
        return tuple((self.baseline or {}).get("selected_skills", ()))

    def record(self, value: dict[str, Any]) -> tuple[bool, list[str]]:
        errors = _validation_errors(value)
        self.attempts.append({
            "attempt": len(self.attempts) + 1,
            "ambiguity_types": list(value["ambiguity_types"]),
            "selected_skills": list(value["selected_skills"]),
            "passed": not errors,
            "errors": errors,
        })
        if errors:
            return False, errors
        self.baseline = json.loads(json.dumps(value, ensure_ascii=False))
        self.approved = True
        return True, []

    def context_message(self) -> str:
        if not self.approved or self.baseline is None:
            raise ValueError("requirement baseline is not approved")
        return "RequirementBaseline: " + json.dumps(self.baseline, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def to_checkpoint(self) -> dict[str, Any]:
        return {
            "ontology_version": ONTOLOGY_VERSION,
            "approved": self.approved,
            "baseline": self.baseline,
            "attempts": self.attempts,
            "context_injected": self.context_injected,
        }

    def restore(self, value: dict[str, Any]) -> None:
        if value.get("ontology_version") != ONTOLOGY_VERSION:
            raise ValueError("resume refused: requirement ontology changed")
        self.approved = value.get("approved") is True
        self.baseline = value.get("baseline")
        self.attempts = list(value.get("attempts", []))
        self.context_injected = value.get("context_injected") is True
        if self.approved and (not isinstance(self.baseline, dict) or _validation_errors(self.baseline)):
            raise ValueError("resume refused: requirement baseline is invalid")

    def trace(self) -> dict[str, Any]:
        baseline = self.baseline or {}
        return {
            "ontology_version": ONTOLOGY_VERSION,
            "ambiguity_types": baseline.get("ambiguity_types", []),
            "selected_skills": baseline.get("selected_skills", []),
            "skill_catalog": list(SKILL_CATALOG),
            "slot_statuses": {name: slot.get("status") for name, slot in baseline.get("slots", {}).items()},
            "evidence": {name: slot.get("evidence", []) for name, slot in baseline.get("slots", {}).items()},
            "gate": {"passed": self.approved, "attempts": self.attempts},
            "original_summary": baseline.get("original_summary", self.task),
            "refined_summary": baseline.get("refined_summary", ""),
        }
