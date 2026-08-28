from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .evidence import sanitize, select_current_runs
from .persistence import atomic_json, write_text_lf
from .schema import validate_json


@dataclass(frozen=True)
class ReportPaths:
    machine_json: Path
    machine_jsonl: Path
    markdown: Path


def _load(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        from .errors import EvalError
        raise EvalError(f"Cannot read report input {path.name}: {exc}", hint="Pass a retained schema-valid validate-all run directory") from exc


def _relative_child(base: Path, value: str) -> Path:
    candidate = Path(value)
    resolved = candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
    boundary = base.parent.resolve()
    if resolved != boundary and boundary not in resolved.parents:
        raise ValueError("child run directory escapes iteration run root")
    return resolved


def _failure_stage(result: dict[str, Any]) -> str:
    error = result.get("error") or {}
    if error.get("stage"):
        return str(error["stage"])
    for stage, status in result.get("stages", {}).items():
        if status in {"failed", "timeout", "invalid"}:
            return stage
    return "none"


def generate_report(run_directory: Path, *, expected_instance_ids: list[str] | None = None) -> ReportPaths:
    run_directory = run_directory.resolve()
    state = _load(run_directory / "validate-all-state.json")
    leaves = select_current_runs(state.get("children", []))
    rows: list[dict[str, Any]] = []
    for child in leaves:
        if child.get("run_type") not in {"replay_noop", "replay_gold"}:
            continue
        child_dir = _relative_child(run_directory, child["run_directory"])
        summary = _load(child_dir / "summary.json")
        validate_json(summary, "validation-summary")
        for item in summary["results"]:
            result_path = Path(item["result"])
            if result_path.is_absolute() or ".." in result_path.parts:
                raise ValueError("unsafe replay result path")
            result = _load(child_dir / result_path)
            validate_json(result, "replay-result")
            rows.append({
                "instance_id": result["instance_id"], "mode": result["mode"], "status": result["status"],
                "failed_stage": _failure_stage(result), "classification": result["classification"],
                "run_id": child["run_id"], "attempts": int(child.get("attempts", 1)),
                "result": f"{child['run_id']}/{result_path.as_posix()}",
                "wall_time_s": result["wall_time_s"], "tests_executed": result["tests_executed"],
            })
    rows.sort(key=lambda row: (row["instance_id"], row["mode"]))
    expected = sorted(expected_instance_ids or {row["instance_id"] for row in rows})
    if len(expected) != 15 or len(set(expected)) != 15:
        raise ValueError("report requires exactly 15 unique instances")
    expected_cells = {(instance, mode) for instance in expected for mode in ("noop", "gold")}
    actual_cells = {(row["instance_id"], row["mode"]) for row in rows}
    if actual_cells != expected_cells or len(rows) != 30:
        raise ValueError("report requires one current noop and gold result for every instance")
    passed = sum(row["status"] == "passed" for row in rows)
    report = {"schema_version": "1.0", "run_id": state["run_id"], "status": "passed" if passed == 30 else "failed", "counts": {"expected": 30, "passed": passed, "failed": 30 - passed}, "results": rows}
    report = sanitize(report, project_root=run_directory)
    machine = run_directory / "validation-summary.json"
    jsonl = run_directory / "results.jsonl"
    markdown = run_directory / "noop-gold-matrix.md"
    atomic_json(machine, report)
    write_text_lf(jsonl, "".join(json.dumps(row, ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n" for row in rows))
    by_cell = {(row["instance_id"], row["mode"]): row for row in rows}
    lines = ["# Iteration 1 no-op / gold matrix", "", "| instance_id | no-op | gold | failure stage |", "| --- | --- | --- | --- |"]
    for instance in expected:
        noop, gold = by_cell[(instance, "noop")], by_cell[(instance, "gold")]
        stages = sorted({r["failed_stage"] for r in (noop, gold) if r["status"] != "passed"})
        lines.append(f"| {instance} | {noop['status']} | {gold['status']} | {', '.join(stages) or '-'} |")
    write_text_lf(markdown, "\n".join(lines) + "\n")
    return ReportPaths(machine, jsonl, markdown)


def publish_audit(run_directory: Path, destination: Path, *, test_summary: str) -> list[Path]:
    run_directory, destination = run_directory.resolve(), destination.resolve()
    state = _load(run_directory / "validate-all-state.json")
    summary = sanitize(_load(run_directory / "validation-summary.json"), project_root=run_directory.parent.parent.parent.parent)
    matrix = (run_directory / "noop-gold-matrix.md").read_text(encoding="utf-8")
    children = select_current_runs(state.get("children", []))
    manifest = {"schema_version": "1.0", "validate_all_run_id": state["run_id"], "runs": [
        {"run_id": child["run_id"], "run_type": child["run_type"], "status": state.get("stages", {}).get(child["run_type"], {}).get("status", "unknown"), "supersedes": child.get("supersedes", [])}
        for child in children if child.get("run_type") in {"replay_noop", "replay_gold"}
    ]}
    proof = state.get("stages", {}).get("isolation", {}).get("proof", {"status": "not_run"})
    destination.mkdir(parents=True, exist_ok=True)
    atomic_json(destination / "validation-summary.json", summary)
    write_text_lf(destination / "noop-gold-matrix.md", sanitize(matrix, project_root=run_directory))
    atomic_json(destination / "run-manifest.json", sanitize(manifest, project_root=run_directory))
    write_text_lf(destination / "test-summary.txt", sanitize(test_summary.strip() + "\n", project_root=run_directory))
    atomic_json(destination / "isolation-proof.json", sanitize(proof, project_root=run_directory))
    return [destination / name for name in ("validation-summary.json", "noop-gold-matrix.md", "run-manifest.json", "test-summary.txt", "isolation-proof.json")]
