from __future__ import annotations

import json
from pathlib import Path

from evalsys.baseline import build_formal_plan
from evalsys.cli import build_parser
from evalsys.harness_environment import harness_receipt_path
from evalsys.iteration3 import (
    ITERATION,
    baseline_snapshots,
    build_iteration3_plan,
    development_smoke_cell,
    iteration_roots,
    requirement_snapshot_payloads,
    summarize_comparison,
    write_test_receipt,
)


ROOT = Path(__file__).resolve().parents[1]


def records() -> list[dict]:
    return [json.loads(line) for line in (ROOT / "benchmark/manifests/paired-cases.jsonl").read_text(encoding="utf-8").splitlines()]


def test_harness_receipt_path_is_iteration_scoped(tmp_path: Path):
    assert harness_receipt_path(tmp_path, iteration=2) == tmp_path / "audit/iteration2/harness-environment-receipt.json"
    assert harness_receipt_path(tmp_path, iteration=3) == tmp_path / "audit/iteration3/harness-environment-receipt.json"


def test_cli_iteration_defaults_to_two_and_accepts_three():
    parser = build_parser()
    assert parser.parse_args(["run-dev", "--version", "v001", "--config", "agent.json", "--confirm"]).iteration == 2
    assert parser.parse_args(["run-dev", "--version", "v001", "--config", "agent.json", "--iteration", "3", "--confirm"]).iteration == 3
    assert parser.parse_args(["freeze-baseline", "--name", "baseline-v2", "--dev-version", "v001", "--config", "agent.json", "--iteration", "3", "--confirm"]).iteration == 3
    assert parser.parse_args(["run-formal", "--name", "baseline-v2", "--iteration", "3", "--confirm"]).iteration == 3
    assert parser.parse_args(["report", "--name", "baseline-v2", "--iteration", "3"]).iteration == 3


def test_iteration3_uses_independent_roots_and_specificity_smoke_cell(tmp_path: Path):
    roots = iteration_roots(tmp_path)
    assert ITERATION == 3
    assert roots == {
        "raw": tmp_path / "artifacts/runs/iteration3",
        "audit": tmp_path / "audit/iteration3",
    }
    cell = development_smoke_cell(records())
    assert cell["case_id"] == "D-S1-fuzzy"
    assert cell["prompt_variant"] == "fuzzy"
    assert cell["ambiguity_type"] == "specificity_reduction"


def test_iteration3_plan_preserves_frozen_cells_and_relables_experiments():
    test_records = [row for row in records() if row["split"] == "test"]
    prior = build_formal_plan(test_records)
    candidate = build_iteration3_plan(test_records)
    assert [{key: row[key] for key in row if key != "experiment"} for row in candidate] == [
        {key: row[key] for key in row if key != "experiment"} for row in prior
    ]
    assert {row["experiment"] for row in candidate if row["variant"] == "full"} == {"E3"}
    assert {row["experiment"] for row in candidate if row["variant"] == "fuzzy"} == {"E4"}


def test_requirement_snapshot_payloads_are_separate_and_versioned():
    payloads = requirement_snapshot_payloads()
    assert set(payloads) == {"requirement-ontology.json", "skill-catalog.json", "reflection-gate.json"}
    assert payloads["requirement-ontology.json"]["version"] == "coding-requirement-ontology-v1"
    assert len(payloads["skill-catalog.json"]) == 3
    assert payloads["reflection-gate.json"]["max_selected_skills"] == 2


def test_baseline_snapshots_bind_ontology_skills_reflection_and_prompts(tmp_path: Path):
    project = tmp_path / "project"
    for relative, content in {
        "prompts/baseline/system.txt": "system\n",
        "prompts/baseline/protocol.txt": "protocol\n",
        "src/reqagent/requirements.py": "requirements\n",
    }.items():
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    snapshots = baseline_snapshots(project)
    assert set(snapshots) == {
        "system_prompt_sha256",
        "protocol_prompt_sha256",
        "requirement_ontology_sha256",
        "skill_catalog_sha256",
        "reflection_gate_sha256",
    }
    assert all(len(value) == 64 for value in snapshots.values())


def test_targeted_receipt_records_profile_and_iteration3_path(tmp_path: Path):
    reference = write_test_receipt(
        tmp_path,
        {"behavior_tree_sha256": "a" * 64},
        command="pytest targeted",
        exit_code=0,
        counts={"passed": 5, "skipped": 1},
    )
    assert reference["path"] == "audit/iteration3/test-receipt.json"
    payload = json.loads((tmp_path / reference["path"]).read_text(encoding="utf-8"))
    assert payload["profile"] == "iteration3_targeted" and payload["status"] == "passed"


def test_comparison_reports_transitions_categories_and_full_regression():
    categories = ["omission"] * 4 + ["referential_ambiguity"] * 4 + ["specificity_reduction"] * 4
    prior_cells = []
    candidate_cells = []
    for index, category in enumerate(categories):
        instance = f"case-{index}"
        prior_cells.extend([
            {"instance_id": instance, "variant": "full", "status": "resolved" if index < 9 else "unresolved", "ambiguity_type": None},
            {"instance_id": instance, "variant": "fuzzy", "status": "resolved" if index < 8 else "unresolved", "ambiguity_type": category},
        ])
        candidate_cells.extend([
            {"instance_id": instance, "variant": "full", "status": "resolved" if index < 9 else "unresolved", "ambiguity_type": None},
            {"instance_id": instance, "variant": "fuzzy", "status": "resolved" if index < 9 else "unresolved", "ambiguity_type": category},
        ])
    comparison = summarize_comparison(
        {"E1_resolved": {"count": 9, "total": 12}, "E2_resolved": {"count": 8, "total": 12}, "cells": prior_cells},
        {"E3_resolved": {"count": 9, "total": 12}, "E4_resolved": {"count": 9, "total": 12}, "cells": candidate_cells},
    )
    assert comparison["resolved"]["E4"] == {"count": 9, "total": 12, "rate": 0.75}
    assert comparison["full_performance_degraded"] is False
    assert comparison["paired_transitions"]["fuzzy"]["unresolved_to_resolved"] == 1
    assert comparison["categories"]["specificity_reduction"]["E2"] == {"count": 0, "total": 4}
    assert comparison["categories"]["specificity_reduction"]["E4"] == {"count": 1, "total": 4}
