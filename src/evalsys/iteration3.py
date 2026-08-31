from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from reqagent.requirements import ONTOLOGY, ONTOLOGY_VERSION, SKILL_CATALOG

from .baseline import FORMAL_SEED, build_formal_plan
from .errors import EvalError
from .recovery import sha256_file


ITERATION = 3
DEVELOPMENT_CASE_ID = "D-S1-fuzzy"


def iteration_roots(project_root: Path) -> dict[str, Path]:
    return {
        "raw": project_root / "artifacts/runs/iteration3",
        "audit": project_root / "audit/iteration3",
    }


def development_smoke_cell(records: list[dict[str, Any]]) -> dict[str, Any]:
    matches = [
        row for row in records
        if row.get("case_id") == DEVELOPMENT_CASE_ID
        and row.get("split") == "dev"
        and row.get("prompt_variant") == "fuzzy"
        and row.get("ambiguity_type") == "specificity_reduction"
    ]
    if len(matches) != 1:
        raise EvalError("iteration3 development smoke cell identity is invalid", category="invalid")
    return matches[0]


def build_iteration3_plan(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    plan = build_formal_plan(records, seed=FORMAL_SEED)
    return [
        {**cell, "experiment": "E3" if cell["variant"] == "full" else "E4"}
        for cell in plan
    ]


def verify_iteration3_plan(plan: list[dict[str, Any]], records: list[dict[str, Any]]) -> None:
    if plan != build_iteration3_plan(records):
        raise EvalError("iteration3 formal plan does not match the frozen paired plan", category="invalid")


def summarize_iteration3_results(rows: list[dict[str, Any]]) -> dict[str, Any]:
    from .iteration2 import summarize_formal_results

    legacy = summarize_formal_results(rows)
    return {
        **{key: value for key, value in legacy.items() if key not in {"E1_resolved", "E2_resolved", "absolute_drop"}},
        "E3_resolved": legacy["E1_resolved"],
        "E4_resolved": legacy["E2_resolved"],
        "absolute_drop": legacy["absolute_drop"],
        "experiment_labels": {"full": "E3", "fuzzy": "E4"},
    }


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def requirement_snapshot_payloads() -> dict[str, Any]:
    return {
        "requirement-ontology.json": {"version": ONTOLOGY_VERSION, "ontology": ONTOLOGY},
        "skill-catalog.json": SKILL_CATALOG,
        "reflection-gate.json": {
            "required": ["goal", "target_component", "expected_behavior_or_acceptance_criteria"],
            "assumption_provenance": True,
            "repository_evidence": True,
            "forbidden_external_or_evaluator_data": True,
            "honest_slot_status": True,
            "max_selected_skills": 2,
        },
    }


def baseline_snapshots(project_root: Path) -> dict[str, str]:
    system = project_root / "prompts/baseline/system.txt"
    protocol = project_root / "prompts/baseline/protocol.txt"
    source = project_root / "src/reqagent/requirements.py"
    if not all(path.is_file() for path in (system, protocol, source)):
        raise EvalError("ReqRefine behavior snapshots are incomplete", category="invalid")
    payloads = requirement_snapshot_payloads()
    return {
        "system_prompt_sha256": hashlib.sha256(system.read_bytes()).hexdigest(),
        "protocol_prompt_sha256": hashlib.sha256(protocol.read_bytes()).hexdigest(),
        "requirement_ontology_sha256": _canonical_hash(payloads["requirement-ontology.json"]),
        "skill_catalog_sha256": _canonical_hash(payloads["skill-catalog.json"]),
        "reflection_gate_sha256": _canonical_hash(payloads["reflection-gate.json"]),
    }


def _rate(value: dict[str, Any]) -> dict[str, Any]:
    count = int(value["count"])
    total = int(value["total"])
    return {"count": count, "total": total, "rate": count / total if total else 0.0}


def _by_cell(cells: list[dict[str, Any]], variant: str) -> dict[str, dict[str, Any]]:
    result = {row["instance_id"]: row for row in cells if row.get("variant") == variant}
    if len(result) != 12:
        raise EvalError("comparison requires 12 unique cells per variant", category="invalid")
    return result


def _transitions(before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]) -> dict[str, int]:
    if set(before) != set(after):
        raise EvalError("comparison cell identities differ", category="invalid")
    result = {
        "resolved_to_resolved": 0,
        "resolved_to_unresolved": 0,
        "unresolved_to_resolved": 0,
        "unresolved_to_unresolved": 0,
    }
    for identity in sorted(before):
        old = before[identity].get("status") == "resolved"
        new = after[identity].get("status") == "resolved"
        key = (
            "resolved_to_resolved" if old and new else
            "resolved_to_unresolved" if old else
            "unresolved_to_resolved" if new else
            "unresolved_to_unresolved"
        )
        result[key] += 1
    return result


def _category_counts(cells: list[dict[str, Any]], variant: str) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for row in cells:
        if row.get("variant") != variant:
            continue
        category = row.get("ambiguity_type")
        if not isinstance(category, str) or not category:
            continue
        counts = result.setdefault(category, {"count": 0, "total": 0})
        counts["total"] += 1
        counts["count"] += int(row.get("status") == "resolved")
    return result


def write_test_receipt(
    project_root: Path,
    bindings: dict[str, str],
    *,
    command: str,
    exit_code: int,
    counts: dict[str, int],
) -> dict[str, str]:
    from .evidence import sanitize

    status = "passed" if exit_code == 0 and counts.get("passed", 0) > 0 else "failed"
    path = project_root / "audit/iteration3/test-receipt.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "profile": "iteration3_targeted",
        "status": status,
        **bindings,
        "command": command,
        "exit_code": exit_code,
        "counts": counts,
    }
    path.write_text(
        json.dumps(sanitize(payload, project_root=project_root), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return {"path": path.relative_to(project_root).as_posix(), "sha256": sha256_file(path)}


def summarize_comparison(previous: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    previous_cells = previous.get("cells", [])
    candidate_cells = candidate.get("cells", [])
    full_before = _by_cell(previous_cells, "full")
    full_after = _by_cell(candidate_cells, "full")
    fuzzy_before = _by_cell(previous_cells, "fuzzy")
    fuzzy_after = _by_cell(candidate_cells, "fuzzy")
    prior_categories = _category_counts(previous_cells, "fuzzy")
    current_categories = _category_counts(candidate_cells, "fuzzy")
    categories = {
        category: {"E2": prior_categories.get(category, {"count": 0, "total": 0}), "E4": current_categories.get(category, {"count": 0, "total": 0})}
        for category in sorted(set(prior_categories) | set(current_categories))
    }
    resolved = {
        "E1": _rate(previous["E1_resolved"]),
        "E2": _rate(previous["E2_resolved"]),
        "E3": _rate(candidate["E3_resolved"]),
        "E4": _rate(candidate["E4_resolved"]),
    }
    return {
        "schema_version": "1.0",
        "statement": "Iteration three introduced structured requirement refinement from aggregate fuzzy-task error analysis and compares it in the same controlled environment.",
        "resolved": resolved,
        "full_performance_degraded": resolved["E3"]["count"] < resolved["E1"]["count"],
        "paired_transitions": {
            "full": _transitions(full_before, full_after),
            "fuzzy": _transitions(fuzzy_before, fuzzy_after),
        },
        "categories": categories,
    }
