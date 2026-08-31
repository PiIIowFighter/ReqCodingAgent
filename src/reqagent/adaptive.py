from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any


BRIEF_LIMIT_BYTES = 3072
ROUTER_VERSION = "adaptive-evidence-router-v1"


@dataclass(frozen=True)
class RouteDecision:
    mode: str
    reasons: tuple[str, ...]
    selected_skills: tuple[str, ...] = ()
    version: str = ROUTER_VERSION


def _selected_skills(reasons: tuple[str, ...]) -> tuple[str, ...]:
    selected = []
    if "unresolved_reference" in reasons:
        selected.append("reference_resolution")
    if any(reason in reasons for reason in ("weak_language", "abstract_behavior")):
        selected.append("specificity_expansion")
    if any(reason in reasons for reason in ("goal", "target", "observable_behavior", "validation")):
        selected.append("omission_recovery")
    return tuple(dict.fromkeys(selected))[:2]


def _decision(mode: str, reasons: tuple[str, ...]) -> RouteDecision:
    return RouteDecision(mode, reasons, _selected_skills(reasons) if mode == "refine" else ())


def route_task(task: str) -> RouteDecision:
    try:
        text = task.strip()
        if not text:
            return _decision("fast", ("router_fail_open_empty_task",))
        lower = text.lower()
        words = text.split()
        weak_language = bool(re.search(r"\b(related|relevant)\b|correctly|improve|better support|handle.*properly", lower))
        abstract_request = bool(re.search(
            r"\b(additional|interactive|consistent|incorrect|standard|appropriate)\b.{0,48}\b(option|behavior|value|interface|handling)\b|\bmishandles?\b",
            lower,
        ))
        unresolved_reference = bool(re.search(r"\b(it|this|that|these|those)\b", lower)) and len(words) < 80
        target = bool(re.search(r"(?:\b[\w./-]+\.py\b|\b[A-Za-z_]\w*\s*\(|`[^`]+`|\bclass\s+\w+|\bfunction\s+\w+)", text))
        observable = bool(re.search(r"\b(return|raise|output|emit|preserve|unchanged|accept|reject|equals?|becomes?|should|expected|when|if)\b", lower))
        contrast = bool(re.search(r"\b(currently|instead|but|however|rather than|fails?|error|regression)\b", lower))
        validation = bool(re.search(r"\b(test|tests|testing|check|verify|pytest|compile|validation)\b", lower)) or (observable and contrast)
        goal = len(words) >= 10 and (observable or contrast or bool(re.search(r"\b(fix|add|update|change|remove|support|implement|ensure|make)\b", lower)))
        detailed = len(words) >= 60 and target and observable and contrast
        missing = tuple(name for name, present in (("goal", goal), ("target", target), ("observable_behavior", observable), ("validation", validation)) if not present)
        if detailed and not abstract_request:
            return _decision("fast", ("detailed_behavior_contract",))
        if weak_language or abstract_request or unresolved_reference or len(missing) >= 2:
            reasons = (("weak_language",) if weak_language else ()) + (("abstract_behavior",) if abstract_request else ()) + (("unresolved_reference",) if unresolved_reference else ()) + missing
            return _decision("refine", reasons)
        return _decision("fast", ("task_is_actionable",))
    except Exception:
        return _decision("fast", ("router_fail_open",))


_SKILL_POLICIES = {
    "omission_recovery": {
        "evidence_order": ("test", "api", "caller", "symmetric_operation"),
        "required_outputs": ("input_output_relation", "boundary", "regression_invariant", "acceptance_check"),
        "focused_searches": 1,
    },
    "reference_resolution": {
        "evidence_order": ("symbol", "caller", "test", "documentation"),
        "required_outputs": ("candidate_referents", "scores", "margin", "chosen_or_uncertain"),
        "focused_searches": 2,
    },
    "specificity_expansion": {
        "evidence_order": ("current_behavior", "normal_path", "boundary_path", "preserved_behavior"),
        "required_outputs": ("input_behavior_output", "pre_check", "post_check"),
        "focused_searches": 1,
    },
}


def evidence_policy(skill_id: str) -> dict[str, Any]:
    if skill_id not in _SKILL_POLICIES:
        raise ValueError("unknown adaptive skill")
    return _SKILL_POLICIES[skill_id]


def adaptive_policy_snapshot() -> dict[str, Any]:
    return {
        "router_version": ROUTER_VERSION,
        "selection_order": ["reference_resolution", "specificity_expansion", "omission_recovery"],
        "selection_rules": {
            "unresolved_reference": "reference_resolution",
            "weak_or_abstract_behavior": "specificity_expansion",
            "missing_requirement_dimension": "omission_recovery",
        },
        "max_selected_skills": 2,
        "policies": _SKILL_POLICIES,
    }


def rank_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not 1 <= len(candidates) <= 3:
        raise ValueError("candidate count must be between one and three")
    result = []
    for candidate in candidates:
        scores = [int(candidate[name]) for name in ("task_fit", "repository_support", "compatibility", "testability")]
        if any(score < 0 or score > 4 for score in scores):
            raise ValueError("candidate scores must be in [0, 4]")
        result.append({**candidate, "score": sum(scores)})
    return sorted(result, key=lambda item: (-item["score"], item["interpretation"]))


def brief_schema() -> dict[str, Any]:
    candidate = {
        "type": "object",
        "properties": {
            "interpretation": {"type": "string", "minLength": 1, "maxLength": 400},
            "task_fit": {"type": "integer", "minimum": 0, "maximum": 4},
            "repository_support": {"type": "integer", "minimum": 0, "maximum": 4},
            "compatibility": {"type": "integer", "minimum": 0, "maximum": 4},
            "testability": {"type": "integer", "minimum": 0, "maximum": 4},
        },
        "required": ["interpretation", "task_fit", "repository_support", "compatibility", "testability"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "ambiguity_reason": {"type": "string", "minLength": 1, "maxLength": 400},
            "chosen_interpretation": {"type": "string", "minLength": 1, "maxLength": 600},
            "targets": {"type": "array", "items": {"type": "string", "maxLength": 160}, "maxItems": 6},
            "expected_behavior": {"type": "string", "minLength": 1, "maxLength": 600},
            "regression_invariants": {"type": "array", "items": {"type": "string", "maxLength": 240}, "maxItems": 6},
            "validation_plan": {"type": "array", "items": {"type": "string", "maxLength": 240}, "minItems": 1, "maxItems": 6},
            "unresolved_uncertainty": {"type": "array", "items": {"type": "string", "maxLength": 240}, "maxItems": 4},
            "evidence_ids": {"type": "array", "items": {"type": "string", "pattern": "^E[0-9]{3}$"}, "minItems": 1, "maxItems": 12, "uniqueItems": True},
            "candidates": {"type": "array", "items": candidate, "minItems": 1, "maxItems": 3},
        },
        "required": ["ambiguity_reason", "chosen_interpretation", "targets", "expected_behavior", "regression_invariants", "validation_plan", "unresolved_uncertainty", "evidence_ids", "candidates"],
        "additionalProperties": False,
    }


@dataclass
class AdaptiveRefinementState:
    task: str
    route: RouteDecision = field(init=False)
    phase: str = field(init=False)
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    brief: dict[str, Any] | None = None
    ranked_candidates: list[dict[str, Any]] = field(default_factory=list)
    revision_count: int = 0
    reflection_count: int = 0
    reflection: dict[str, Any] | None = None
    requires_reflection: bool = False
    contradiction_reason: str = ""
    schema_removed: bool = False
    usage_by_phase: dict[str, dict[str, int]] = field(default_factory=lambda: {
        "router": {}, "refinement": {}, "main": {}, "reflection": {},
    })
    steps_by_phase: dict[str, int] = field(default_factory=lambda: {
        "router": 0, "refinement": 0, "main": 0, "reflection": 0,
    })
    tools_by_phase: dict[str, int] = field(default_factory=lambda: {
        "router": 0, "refinement": 0, "main": 0, "reflection": 0,
    })
    patch_seen: bool = False
    fallback_reason: str = ""

    def __post_init__(self) -> None:
        self.route = route_task(self.task)
        self.phase = "coding" if self.route.mode == "fast" else "refining"

    def refinement_instruction(self) -> str:
        policies = []
        for skill_id in self.route.selected_skills:
            policy = evidence_policy(skill_id)
            policies.append(
                f"{skill_id}: evidence_order={','.join(policy['evidence_order'])}; "
                f"required_outputs={','.join(policy['required_outputs'])}"
            )
        return "Adaptive refinement phase. Selected policies: " + " | ".join(policies) + ". Compare at most three interpretations, use only evidence IDs from repository tools, then record a compact RequirementBrief."

    def fail_open_refinement(self, reason: str) -> str:
        self.phase = "coding"
        self.schema_removed = True
        self.fallback_reason = reason[:500]
        return "Refinement failed open. Continue with the original task and the six baseline tools."

    def add_evidence(self, tool: str, data: dict[str, Any]) -> str:
        evidence_id = f"E{len(self.evidence) + 1:03d}"
        digest = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        self.evidence[evidence_id] = {"tool": tool, "sha256": digest, "summary": _evidence_summary(tool, data)}
        return evidence_id

    def record_brief(self, value: dict[str, Any]) -> tuple[bool, list[str]]:
        errors = []
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        if len(payload) > BRIEF_LIMIT_BYTES:
            errors.append("RequirementBrief exceeds 3072 bytes")
        unknown = sorted(set(value.get("evidence_ids", ())) - set(self.evidence))
        if unknown:
            errors.append("unknown evidence IDs: " + ", ".join(unknown))
        try:
            ranked = rank_candidates(value.get("candidates", []))
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(str(exc))
            ranked = []
        chosen = value.get("chosen_interpretation", "")
        if ranked and ranked[0]["interpretation"] != chosen and ranked[0]["interpretation"] not in chosen:
            errors.append("chosen interpretation is not the top-ranked candidate")
        if errors:
            return False, errors
        self.brief = json.loads(payload.decode())
        self.ranked_candidates = ranked
        self.phase = "coding"
        self.schema_removed = True
        return True, []

    def brief_message(self) -> str:
        if self.brief is None:
            return ""
        return "Refinement is complete. RequirementBrief (evidence-backed, revisable working hypothesis; original task has priority). Continue with the baseline coding loop: " + json.dumps(self.brief, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def observe_tool(self, name: str, ok: bool, error: dict[str, Any] | None, diff_changed: bool) -> None:
        if name == "apply_patch" and ok and diff_changed:
            self.patch_seen = True
        if self.route.mode != "refine" or not self.patch_seen:
            return
        if name == "run_command" and not ok:
            kind = (error or {}).get("kind", "command_failed")
            self.requires_reflection = True
            self.contradiction_reason = f"post-patch validation returned {kind}"
        if name == "apply_patch" and ok and not diff_changed:
            self.requires_reflection = True
            self.contradiction_reason = "post-patch diff made no observable change"
        if self.requires_reflection:
            if self.reflection_count >= 1:
                self.fail_open_reflection("reflection already completed; retain current best patch")
            else:
                self.phase = "reflection"

    def reflect(self, decision: str, reason: str) -> tuple[bool, str]:
        if decision not in {"accept", "revise"} or not reason.strip():
            return False, "reflection requires accept/revise and a reason"
        if self.reflection_count >= 1:
            self.phase = "accepted"
            self.reflection = {"decision": "accept", "reason": "reflection already completed; accept current best", "revision_count": self.revision_count}
            return True, ""
        self.reflection_count += 1
        if decision == "revise":
            if self.revision_count >= 1:
                self.phase = "accepted"
                self.reflection = {"decision": "accept", "reason": "revision limit reached; accept current best", "revision_count": self.revision_count}
                return True, ""
            self.revision_count += 1
            self.phase = "refining"
            self.brief = None
            self.schema_removed = False
        else:
            self.phase = "accepted"
        self.reflection = {"decision": decision, "reason": reason[:500], "revision_count": self.revision_count}
        return True, ""

    def fail_open_reflection(self, reason: str) -> None:
        self.phase = "accepted"
        self.reflection_count = max(1, self.reflection_count)
        self.reflection = {"decision": "accept", "reason": f"reflection fail-open: {reason}"[:500], "revision_count": self.revision_count}

    def phase_for_model(self) -> str:
        if self.route.mode == "fast":
            return "main"
        if self.phase == "refining":
            return "refinement"
        if self.phase == "reflection":
            return "reflection"
        return "main"

    def add_usage(self, phase: str, usage: dict[str, int]) -> None:
        target = self.usage_by_phase[phase]
        for key, value in usage.items():
            target[key] = target.get(key, 0) + value

    def to_checkpoint(self) -> dict[str, Any]:
        return {
            "version": ROUTER_VERSION, "task": self.task, "route": self.route.__dict__, "phase": self.phase,
            "evidence": self.evidence, "brief": self.brief, "ranked_candidates": self.ranked_candidates,
            "revision_count": self.revision_count, "reflection_count": self.reflection_count,
            "reflection": self.reflection, "requires_reflection": self.requires_reflection,
            "contradiction_reason": self.contradiction_reason, "schema_removed": self.schema_removed,
            "usage_by_phase": self.usage_by_phase, "steps_by_phase": self.steps_by_phase,
            "tools_by_phase": self.tools_by_phase, "patch_seen": self.patch_seen,
            "fallback_reason": self.fallback_reason,
        }

    def restore(self, value: dict[str, Any]) -> None:
        required = {"version", "task", "route", "phase", "evidence", "brief", "ranked_candidates", "revision_count", "reflection_count", "reflection", "requires_reflection", "contradiction_reason", "schema_removed", "usage_by_phase", "steps_by_phase", "tools_by_phase", "patch_seen", "fallback_reason"}
        if set(value) != required or value.get("version") != ROUTER_VERSION or value.get("task") != self.task:
            raise ValueError("resume refused: adaptive refinement identity changed")
        raw_route = value["route"]
        route = RouteDecision(
            mode=raw_route["mode"],
            reasons=tuple(raw_route["reasons"]),
            selected_skills=tuple(raw_route.get("selected_skills", ())),
            version=raw_route.get("version", ROUTER_VERSION),
        )
        if route != route_task(self.task) or value["phase"] not in {"refining", "coding", "reflection", "accepted"}:
            raise ValueError("resume refused: invalid adaptive phase or route")
        if value["schema_removed"] and value["phase"] == "refining":
            raise ValueError("resume refused: refining phase cannot have removed schema")
        self.route = route
        self.phase = value["phase"]
        self.evidence = dict(value["evidence"])
        self.brief = value.get("brief")
        self.ranked_candidates = list(value.get("ranked_candidates", []))
        self.revision_count = int(value["revision_count"])
        self.reflection_count = int(value["reflection_count"])
        self.reflection = value.get("reflection")
        self.requires_reflection = value["requires_reflection"] is True
        self.contradiction_reason = value["contradiction_reason"]
        self.schema_removed = value["schema_removed"] is True
        self.usage_by_phase = {key: dict(item) for key, item in value["usage_by_phase"].items()}
        self.steps_by_phase = dict(value["steps_by_phase"])
        self.tools_by_phase = dict(value["tools_by_phase"])
        self.patch_seen = value["patch_seen"] is True
        self.fallback_reason = value["fallback_reason"]

    def trace(self) -> dict[str, Any]:
        aggregate_usage: dict[str, int] = {}
        for usage in self.usage_by_phase.values():
            for key, value in usage.items():
                aggregate_usage[key] = aggregate_usage.get(key, 0) + value
        return {
            "router": self.route.__dict__, "phase": self.phase, "evidence": self.evidence,
            "skill_policies": _SKILL_POLICIES, "candidates": self.ranked_candidates, "brief": self.brief,
            "schema_removed": self.schema_removed, "reflection": self.reflection,
            "revision_count": self.revision_count, "reflection_count": self.reflection_count,
            "requires_reflection": self.requires_reflection, "contradiction_reason": self.contradiction_reason,
            "fallback_reason": self.fallback_reason, "selected_skills": list(self.route.selected_skills),
            "usage_by_phase": self.usage_by_phase, "usage": aggregate_usage,
            "steps_by_phase": self.steps_by_phase, "tools_by_phase": self.tools_by_phase,
        }


def _evidence_summary(tool: str, data: dict[str, Any]) -> str:
    if tool == "read_file":
        return f"read:{data.get('path', '')}:{data.get('start_line', 1)}-{data.get('end_line', 0)}"
    if tool == "search_text":
        matches = data.get("matches", [])
        return f"search:{len(matches)} matches"
    return f"{tool}:{len(data.get('entries', []))} entries"
