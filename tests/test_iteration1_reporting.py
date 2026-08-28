from __future__ import annotations

import json
from pathlib import Path

import pytest

from evalsys.acceptance import ACCEPTANCE_KEYS, evaluate_acceptance
from evalsys.reporting import generate_report, publish_audit
from evalsys import cli
from evalsys.validate_all import run_validate_all


IDS = [f"repo__task-{i:02d}" for i in range(15)]


def _result(instance_id: str, mode: str, status: str = "passed", stage: str = "none") -> dict:
    return {
        "schema_version": "1.0", "run_id": f"{mode}-run", "case_id": instance_id + "-full",
        "instance_id": instance_id, "split": "test" if instance_id != IDS[-1] else "dev", "mode": mode,
        "status": status, "classification": "expected_test_statuses", "harness_revision": "a" * 40,
        "data_revision": "b" * 40, "repo": "repo/task", "base_commit": "c" * 40,
        "docker_image": "image@sha256:" + "d" * 64, "started_at": "2026-08-28T00:00:00Z",
        "ended_at": "2026-08-28T00:00:01Z", "wall_time_s": 1.0,
        "stages": {"environment": "passed", "patch": "skipped" if mode == "noop" else "passed", "tests": "failed" if stage == "tests" else "passed"},
        "tests_executed": True, "fail_to_pass": {"a": "FAILED" if mode == "noop" else "PASSED"},
        "pass_to_pass": {"b": "PASSED"}, "logs": {"stdout": "stdout.log", "stderr": "stderr.log", "harness": "harness"},
        "error": None if status == "passed" else {"category": status, "message": "short", "stage": stage},
        "cleanup": {"status": "passed", "message": None},
    }


def _replay(root: Path, run_id: str, mode: str, statuses: dict[str, str] | None = None) -> Path:
    run = root / run_id
    results = []
    for instance_id in IDS:
        status = (statuses or {}).get(instance_id, "passed")
        case = run / "cases" / (instance_id + "-full") / mode
        case.mkdir(parents=True)
        result = _result(instance_id, mode, status, "tests" if status == "test_failed" else "environment")
        result["run_id"] = run_id
        (case / "result.json").write_text(json.dumps(result), encoding="utf-8")
        results.append({"instance_id": instance_id, "mode": mode, "status": status, "result": f"cases/{instance_id}-full/{mode}/result.json"})
    (run / "summary.json").write_text(json.dumps({"schema_version": "1.0", "run_id": run_id, "status": "passed" if all(x["status"] == "passed" for x in results) else "failed", "results": results}), encoding="utf-8")
    (run / "COMPLETE").write_text("{}", encoding="utf-8")
    return run


def test_report_selects_supersession_leaves_and_writes_deterministic_outputs(tmp_path: Path):
    old = _replay(tmp_path, "noop-old", "noop", {IDS[0]: "test_failed"})
    new = _replay(tmp_path, "noop-new", "noop")
    gold = _replay(tmp_path, "gold", "gold")
    validation = tmp_path / "validate"
    validation.mkdir()
    state = {"schema_version": "1.0", "run_id": "validate", "status": "running", "stages": {}, "children": [
        {"run_id": "noop-old", "run_type": "replay_noop", "run_directory": "../noop-old", "validity": "active", "supersedes": []},
        {"run_id": "noop-new", "run_type": "replay_noop", "run_directory": "../noop-new", "validity": "active", "supersedes": ["noop-old"]},
        {"run_id": "gold", "run_type": "replay_gold", "run_directory": "../gold", "validity": "active", "supersedes": []},
    ]}
    (validation / "validate-all-state.json").write_text(json.dumps(state), encoding="utf-8")
    paths = generate_report(validation, expected_instance_ids=IDS)
    machine = json.loads(paths.machine_json.read_text(encoding="utf-8"))
    assert machine["counts"] == {"expected": 30, "passed": 30, "failed": 0}
    assert {row["run_id"] for row in machine["results"]} == {"noop-new", "gold"}
    assert paths.machine_jsonl.read_text(encoding="utf-8").count("\n") == 30
    matrix = paths.markdown.read_text(encoding="utf-8")
    assert matrix.count("\n|") == 17  # header, separator, and exactly 15 data rows
    assert str(tmp_path) not in matrix


def test_report_preserves_failure_stage_retry_and_timeout(tmp_path: Path):
    _replay(tmp_path, "noop", "noop", {IDS[0]: "timeout"})
    _replay(tmp_path, "gold", "gold", {IDS[1]: "test_failed"})
    validation = tmp_path / "validate"
    validation.mkdir()
    (validation / "validate-all-state.json").write_text(json.dumps({"schema_version": "1.0", "run_id": "v", "status": "failed", "stages": {}, "children": [
        {"run_id": "noop", "run_type": "replay_noop", "run_directory": "../noop", "validity": "active", "supersedes": [], "attempts": 2},
        {"run_id": "gold", "run_type": "replay_gold", "run_directory": "../gold", "validity": "active", "supersedes": [], "attempts": 1},
    ]}), encoding="utf-8")
    report = json.loads(generate_report(validation, expected_instance_ids=IDS).machine_json.read_text(encoding="utf-8"))
    rows = {(r["instance_id"], r["mode"]): r for r in report["results"]}
    assert rows[(IDS[0], "noop")]["failed_stage"] == "environment"
    assert rows[(IDS[0], "noop")]["attempts"] == 2
    assert rows[(IDS[0], "noop")]["status"] == "timeout"


def test_validate_all_stops_on_infra_but_continues_after_test_failure(tmp_path: Path):
    calls = []
    def stage(name, context):
        calls.append(name)
        if name == "replay_noop": return {"status": "failed", "failure_kind": "test", "run_id": "n", "run_directory": "n"}
        if name == "replay_gold": return {"status": "passed", "run_id": "g", "run_directory": "g"}
        return {"status": "passed"}
    report = run_validate_all(tmp_path, stage_runner=stage, run_id="v-test")
    assert "replay_gold" in calls and "audit" in calls and "acceptance" in calls and report["status"] == "failed"
    calls.clear()
    def infra(name, context):
        calls.append(name)
        return {"status": "failed", "failure_kind": "infra"} if name == "locks_and_cache" else {"status": "passed"}
    report = run_validate_all(tmp_path, stage_runner=infra, run_id="v-infra")
    assert calls == ["preflight", "locks_and_cache"] and report["status"] == "failed"


def test_validate_all_resume_keeps_failed_child_identity_available(tmp_path: Path):
    def first(name, context):
        if name == "replay_noop":
            return {"status": "failed", "failure_kind": "test", "run_id": "noop-child", "run_directory": "noop-child"}
        return {"status": "passed"}
    run_validate_all(tmp_path, stage_runner=first, run_id="children")
    seen = {}
    def resumed(name, context):
        if name == "replay_noop":
            seen["children"] = list(context["state"]["children"])
        return {"status": "passed"}
    run_validate_all(tmp_path, stage_runner=resumed, run_id="children", resume=True)
    assert seen["children"][0]["run_id"] == "noop-child"


def test_validate_all_resume_keeps_completed_children(tmp_path: Path):
    calls = []
    def first(name, context):
        calls.append(name)
        if name == "strict_data": raise RuntimeError("interrupted")
        return {"status": "passed", "run_id": name, "run_directory": name} if name.startswith("replay_") else {"status": "passed"}
    with pytest.raises(RuntimeError):
        run_validate_all(tmp_path, stage_runner=first, run_id="durable")
    calls.clear()
    run_validate_all(tmp_path, stage_runner=lambda name, context: calls.append(name) or {"status": "passed"}, run_id="durable", resume=True)
    assert calls[0] == "strict_data"
    state = json.loads((tmp_path / "artifacts/runs/iteration1/durable/validate-all-state.json").read_text(encoding="utf-8"))
    assert state["stages"]["preflight"]["status"] == "passed"


def test_publish_audit_derives_required_sanitized_files(tmp_path: Path):
    _replay(tmp_path, "noop", "noop")
    _replay(tmp_path, "gold", "gold")
    validation = tmp_path / "validate"
    validation.mkdir()
    (validation / "validate-all-state.json").write_text(json.dumps({"schema_version": "1.0", "run_id": "v", "status": "passed", "stages": {"isolation": {"status": "passed", "proof": {"project_root_mounted": False}}}, "children": [
        {"run_id": "noop", "run_type": "replay_noop", "run_directory": "../noop", "validity": "active", "supersedes": []},
        {"run_id": "gold", "run_type": "replay_gold", "run_directory": "../gold", "validity": "active", "supersedes": []},
    ]}), encoding="utf-8")
    generate_report(validation, expected_instance_ids=IDS)
    destination = tmp_path / "audit"
    publish_audit(validation, destination, test_summary="107 passed")
    assert {p.name for p in destination.iterdir()} == {"validation-summary.json", "noop-gold-matrix.md", "run-manifest.json", "test-summary.txt", "isolation-proof.json", "checksums.sha256"}
    assert str(tmp_path) not in "".join(p.read_text(encoding="utf-8") for p in destination.iterdir())


def test_readme_and_wrappers_are_bounded_and_quoting_safe():
    root = Path(__file__).parents[1]
    text = (root / "README.txt").read_text(encoding="utf-8")
    assert len(text) <= 1000
    assert all(term in text for term in ("https://github.com/PiIIowFighter/ReqCodingAgent", "report RUN_DIRECTORY", "validate-all", "WSL2", "Docker"))
    shell = (root / "scripts/validate-all.sh").read_text(encoding="utf-8")
    powershell = (root / "scripts/validate-all.ps1").read_text(encoding="utf-8")
    assert '"$@"' in shell and "@Args" in powershell


def test_cli_report_missing_directory_is_actionable_not_traceback(tmp_path: Path, capsys):
    assert cli.main(["--project-root", str(Path(__file__).parents[1]), "report", str(tmp_path / "missing")]) == 2
    captured = capsys.readouterr()
    assert "ERROR" in captured.err and "Traceback" not in captured.err


def test_cli_exposes_report_and_validate_all_resume_options():
    parser = cli.build_parser()
    assert parser.parse_args(["report", "run"]).run_directory == Path("run")
    args = parser.parse_args(["validate-all", "--resume", "--run-id", "v", "--noop-run-id", "n", "--gold-run-id", "g"])
    assert (args.resume, args.run_id, args.noop_run_id, args.gold_run_id) == (True, "v", "n", "g")


def test_acceptance_rejects_fabricated_checks_rows_and_weak_audit(tmp_path: Path):
    fake_rows = [{"instance_id": instance, "mode": mode, "status": "passed"} for instance in IDS for mode in ("noop", "gold")]
    fake = {"run_id": "v", "counts": {"expected": 30, "passed": 30, "failed": 0}, "results": fake_rows, "data_checks": {
        "schema_pairs_12_3": True, "distribution_4_4_4_1_1_1": True, "official_hashes_15": True, "pair_official_fields_equal": True}}
    audit = tmp_path / "audit/iteration1"
    audit.mkdir(parents=True)
    for name in ("validation-summary.json", "run-manifest.json", "isolation-proof.json"):
        (audit / name).write_text("{}", encoding="utf-8")
    for name in ("noop-gold-matrix.md", "test-summary.txt"):
        (audit / name).write_text("placeholder", encoding="utf-8")
    report = evaluate_acceptance(tmp_path, fake, unit_tests_passed=True)
    assert not report["criteria"]["official_hashes_15"]
    assert not report["criteria"]["pair_official_fields_equal"]
    assert not report["criteria"]["executable_isolation"]
    assert not report["criteria"]["sanitized_audit"]
    assert not report["criteria"]["noop_15_passed"]


def test_acceptance_maps_every_section19_item_and_never_completes_without_30(tmp_path: Path):
    report = evaluate_acceptance(tmp_path, {"counts": {"expected": 30, "passed": 29, "failed": 1}, "results": []}, unit_tests_passed=True)
    assert set(report["criteria"]) == set(ACCEPTANCE_KEYS)
    assert report["iteration_completion"] is False
    assert report["criteria"]["noop_15_passed"] is False or report["criteria"]["gold_15_passed"] is False
