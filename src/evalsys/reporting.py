from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .evidence import sanitize, select_current_runs, verify_checksums
from .persistence import atomic_json, write_text_lf
from .schema import validate_json


@dataclass(frozen=True)
class ReportPaths:
    machine_json: Path
    machine_jsonl: Path
    markdown: Path


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        from .errors import EvalError
        raise EvalError(f"Cannot read report input {path.name}: {exc}", hint="Pass retained schema-valid run evidence") from exc
    if not isinstance(value, dict):
        raise ValueError(f"report input {path.name} must be a JSON object")
    return value


def _relative_child(base: Path, child: dict[str, Any]) -> Path:
    candidate = Path(child["run_directory"])
    resolved = candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
    expected = base.parent.resolve() / child["run_id"]
    if resolved != expected or resolved.name != child["run_id"]:
        raise ValueError("child run directory is not bound to iteration root/run_id")
    if not (resolved / "COMPLETE").is_file() or not (resolved / "summary.json").is_file():
        raise ValueError("child run is not complete")
    return resolved


def _failure_stage(result: dict[str, Any]) -> str:
    error = result.get("error") or {}
    if error.get("stage"):
        return str(error["stage"])
    for stage, status in result.get("stages", {}).items():
        if status in {"failed", "timeout", "invalid"}:
            return stage
    return "none"


def _outcome_counts(outcomes: dict[str, str]) -> dict[str, int]:
    counts = {key: 0 for key in ("passed", "failed", "skipped", "error")}
    for status in outcomes.values():
        normalized = "passed" if status == "XFAIL" else status.lower()
        counts[normalized if normalized in counts else "error"] += 1
    return counts


def _smoke_row(run_directory: Path, *, expected_mode: str, validity: str) -> dict[str, Any]:
    run_directory = run_directory.resolve()
    if not (run_directory / "COMPLETE").is_file():
        raise ValueError("smoke source run is not complete")
    summary = _load(run_directory / "summary.json")
    validate_json(summary, "validation-summary")
    if summary["run_id"] != run_directory.name or len(summary["results"]) != 1:
        raise ValueError("smoke source summary identity mismatch")
    if summary["status"] != "passed":
        raise ValueError("smoke source replay summary did not pass")
    item = summary["results"][0]
    result_path = Path(item["result"])
    if result_path.is_absolute() or ".." in result_path.parts:
        raise ValueError("unsafe smoke result path")
    result_file = run_directory / result_path
    case_directory = result_file.parent
    completion = _load(case_directory / "COMPLETE")
    from .recovery import load_reusable_run, sha256_file
    input_fingerprint = completion.get("input_fingerprint")
    if not isinstance(input_fingerprint, str):
        raise ValueError("smoke completion input fingerprint is missing")
    result = load_reusable_run(case_directory, input_fingerprint)
    if result is None:
        raise ValueError("smoke replay checkpoint validation failed")
    identity = (result["run_id"], result["instance_id"], result["mode"], result["status"])
    expected = (summary["run_id"], "django__django-11133", expected_mode, item["status"])
    if identity != expected or (item["instance_id"], item["mode"]) != expected[1:3]:
        raise ValueError("smoke result identity mismatch")
    return {
        "instance_id": result["instance_id"],
        "mode": result["mode"],
        "tests_executed": result["tests_executed"],
        "fail_to_pass": _outcome_counts(result["fail_to_pass"]),
        "pass_to_pass": _outcome_counts(result["pass_to_pass"]),
        "patch_stage": result["stages"]["patch"],
        "cleanup": result["cleanup"],
        "raw_result_sha256": sha256_file(result_file),
        "run_status": result["status"],
        "validity": validity,
        "run_id": result["run_id"],
    }


def generate_smoke_report(noop_run: Path, gold_run: Path, *, destination: Path, validation_receipt: dict[str, Any] | None = None) -> ReportPaths:
    project_root = destination.resolve().parent.parent
    artifact_root = (project_root / "artifacts/runs/iteration1").resolve()
    runs = (noop_run.resolve(), gold_run.resolve())
    if runs[0] == runs[1]:
        raise ValueError("smoke report requires distinct noop and gold runs")
    if any(run.parent != artifact_root for run in runs):
        raise ValueError("smoke runs must be direct children of the iteration artifact root")
    index = _load(project_root / "audit/iteration1/index.json")
    replay_runs = [entry for entry in index.get("runs", []) if entry.get("run_type") in {"replay_noop", "replay_gold"}]
    leaves = {entry["run_id"]: entry for entry in select_current_runs(replay_runs)}
    validities = {}
    for run in runs:
        entry = leaves.get(run.name)
        expected_raw = f"artifacts/runs/iteration1/{run.name}"
        validities[run.name] = "active" if entry and entry.get("status") == "passed" and entry.get("raw_path") == expected_raw else "superseded_or_invalid"
    rows = [
        _smoke_row(runs[0], expected_mode="noop", validity=validities[runs[0].name]),
        _smoke_row(runs[1], expected_mode="gold", validity=validities[runs[1].name]),
    ]
    if {(row["instance_id"], row["mode"]) for row in rows} != {
        ("django__django-11133", "noop"),
        ("django__django-11133", "gold"),
    }:
        raise ValueError("smoke report requires exactly one noop and gold cell")
    rows.sort(key=lambda row: (row["instance_id"], row["mode"]))
    def expected_outcomes(row: dict[str, Any]) -> bool:
        f2p, p2p = row["fail_to_pass"], row["pass_to_pass"]
        common = row["run_status"] == "passed" and row["validity"] == "active" and row["tests_executed"]
        p2p_ok = p2p["failed"] == p2p["skipped"] == p2p["error"] == 0
        if row["mode"] == "noop":
            return common and p2p_ok and f2p["passed"] == f2p["skipped"] == f2p["error"] == 0 and f2p["failed"] > 0
        return common and p2p_ok and f2p["failed"] == f2p["skipped"] == f2p["error"] == 0 and f2p["passed"] > 0
    status = "passed" if all(expected_outcomes(row) for row in rows) else "failed"
    lines = [
        "# Iteration 1 smoke matrix",
        "",
        "| instance_id | mode | status | F2P (P/F/S/E) | P2P (P/F/S/E) | patch | cleanup | validity |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        f2p, p2p = row["fail_to_pass"], row["pass_to_pass"]
        lines.append(f"| {row['instance_id']} | {row['mode']} | {row['run_status']} | {f2p['passed']}/{f2p['failed']}/{f2p['skipped']}/{f2p['error']} | {p2p['passed']}/{p2p['failed']}/{p2p['skipped']}/{p2p['error']} | {row['patch_stage']} | {row['cleanup']['status']} | {row['validity']} |")
    destination.mkdir(parents=True, exist_ok=True)
    machine = destination / "smoke-summary.json"
    markdown = destination / "smoke-matrix.md"
    write_text_lf(markdown, "\n".join(lines) + "\n")
    from .recovery import sha256_file
    payload = {"schema_version": "1.0", "status": status, "matrix_sha256": sha256_file(markdown), "results": rows}
    if validation_receipt is not None:
        validate_json(validation_receipt, "validation-receipt")
        payload["validation_receipt"] = validation_receipt
    atomic_json(machine, sanitize(payload, project_root=project_root))
    return ReportPaths(machine, destination / "results.jsonl", markdown)


def generate_report(run_directory: Path, *, expected_instance_ids: list[str] | None = None) -> ReportPaths:
    run_directory = run_directory.resolve()
    state = _load(run_directory / "validate-all-state.json")
    leaves = select_current_runs(state.get("children", []))
    rows: list[dict[str, Any]] = []
    for child in leaves:
        if child.get("run_type") not in {"replay_noop", "replay_gold"}:
            continue
        child_dir = _relative_child(run_directory, child)
        summary = _load(child_dir / "summary.json")
        validate_json(summary, "validation-summary")
        if summary["run_id"] != child["run_id"]:
            raise ValueError("child summary run_id mismatch")
        for item in summary["results"]:
            result_path = Path(item["result"])
            if result_path.is_absolute() or ".." in result_path.parts:
                raise ValueError("unsafe replay result path")
            result = _load(child_dir / result_path)
            validate_json(result, "replay-result")
            if (result["run_id"], result["instance_id"], result["mode"], result["status"]) != (child["run_id"], item["instance_id"], item["mode"], item["status"]):
                raise ValueError("replay result identity disagrees with child summary")
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
    from .recovery import sha256_file
    names = ("validation-summary.json", "noop-gold-matrix.md", "run-manifest.json", "test-summary.txt", "isolation-proof.json")
    write_text_lf(destination / "checksums.sha256", "".join(f"{sha256_file(destination / name)}  {name}\n" for name in sorted(names)))
    return [destination / name for name in (*names, "checksums.sha256")]
