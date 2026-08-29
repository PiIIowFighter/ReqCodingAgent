from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from evalsys.acceptance import ACCEPTANCE_KEYS, _contains_secret, _is_valid_isolation_proof, _tracked_text, evaluate_acceptance
from evalsys.reporting import _outcome_counts, generate_report, generate_smoke_report, publish_audit
from evalsys import cli
from evalsys.recovery import compute_input_fingerprint, write_completed_run
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


def _smoke_replay(root: Path, run_id: str, mode: str) -> Path:
    run = root / "artifacts/runs/iteration1" / run_id
    case = run / "cases/D-O1-full" / mode
    case.mkdir(parents=True)
    result = _result("django__django-11133", mode)
    result["run_id"] = run_id
    result["case_id"] = "D-O1-full"
    result["split"] = "dev"
    result["fail_to_pass"] = {"f2p-a": "FAILED", "f2p-b": "FAILED"} if mode == "noop" else {"f2p-a": "PASSED", "f2p-b": "PASSED"}
    result["pass_to_pass"] = {"p2p-a": "PASSED", "p2p-b": "PASSED"}
    result["cleanup"] = {"status": "passed", "message": None}
    for name in ("stdout.log", "stderr.log", "events.jsonl", "dataset.json", "prediction.jsonl"):
        (case / name).write_text("{}\n" if name.endswith(".json") else "", encoding="utf-8")
    harness = case / "harness"
    harness.mkdir()
    (harness / "run.log").write_text("fixture", encoding="utf-8")
    fingerprint = compute_input_fingerprint({"run_id": run_id, "mode": mode})
    write_completed_run(
        case,
        result,
        fingerprint,
        ["stdout.log", "stderr.log", "events.jsonl", "dataset.json", "prediction.jsonl"],
        artifact_trees=["harness"],
    )
    (run / "summary.json").write_text(json.dumps({"schema_version": "1.0", "run_id": run_id, "status": "passed", "results": [{"instance_id": "django__django-11133", "mode": mode, "status": "passed", "result": f"cases/D-O1-full/{mode}/result.json"}]}), encoding="utf-8")
    (run / "COMPLETE").write_text("{}", encoding="utf-8")
    audit = root / "audit/iteration1"
    audit.mkdir(parents=True, exist_ok=True)
    index_path = audit / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {"runs": []}
    index["runs"].append({"run_id": run_id, "run_type": f"replay_{mode}", "status": "passed", "raw_path": f"artifacts/runs/iteration1/{run_id}", "validity": "active", "supersedes": []})
    index_path.write_text(json.dumps(index), encoding="utf-8")
    return run


def test_smoke_report_writes_exact_redacted_1x2(tmp_path: Path):
    noop = _smoke_replay(tmp_path, "noop", "noop")
    gold = _smoke_replay(tmp_path, "gold", "gold")
    destination = tmp_path / "audit/iteration1"
    paths = generate_smoke_report(noop, gold, destination=destination)
    report = json.loads(paths.machine_json.read_text(encoding="utf-8"))
    assert {(row["instance_id"], row["mode"]) for row in report["results"]} == {("django__django-11133", "noop"), ("django__django-11133", "gold")}
    rows = {row["mode"]: row for row in report["results"]}
    assert rows["noop"]["fail_to_pass"] == {"passed": 0, "failed": 2, "skipped": 0, "error": 0}
    assert rows["noop"]["pass_to_pass"] == {"passed": 2, "failed": 0, "skipped": 0, "error": 0}
    assert rows["gold"]["fail_to_pass"] == {"passed": 2, "failed": 0, "skipped": 0, "error": 0}
    assert rows["gold"]["pass_to_pass"] == {"passed": 2, "failed": 0, "skipped": 0, "error": 0}
    assert report["status"] == "passed"
    assert all(len(row["raw_result_sha256"]) == 64 for row in report["results"])
    assert not paths.machine_jsonl.exists()
    assert str(tmp_path) not in paths.machine_json.read_text(encoding="utf-8") + paths.markdown.read_text(encoding="utf-8")


def test_smoke_outcome_counts_treats_xfail_as_skipped():
    assert _outcome_counts({"expected-failure": "XFAIL"}) == {
        "passed": 0, "failed": 0, "skipped": 1, "error": 0,
    }


def test_smoke_report_rejects_missing_cell(tmp_path: Path):
    noop = _smoke_replay(tmp_path, "noop", "noop")
    with pytest.raises(ValueError, match="noop and gold"):
        generate_smoke_report(noop, noop, destination=tmp_path / "audit/iteration1")


def test_smoke_report_rejects_wrong_identity(tmp_path: Path):
    noop = _smoke_replay(tmp_path, "noop", "noop")
    gold = _smoke_replay(tmp_path, "gold", "gold")
    result_path = gold / "cases/D-O1-full/gold/result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["instance_id"] = "wrong__task-1"
    result_path.write_text(json.dumps(result), encoding="utf-8")
    complete_path = result_path.parent / "COMPLETE"
    complete = json.loads(complete_path.read_text(encoding="utf-8"))
    complete["artifacts"]["result.json"] = __import__("hashlib").sha256(result_path.read_bytes()).hexdigest()
    complete_path.write_text(json.dumps(complete), encoding="utf-8")
    with pytest.raises(ValueError, match="identity"):
        generate_smoke_report(noop, gold, destination=tmp_path / "audit/iteration1")


def test_cli_exposes_smoke_report_options():
    parser = cli.build_parser()
    args = parser.parse_args(["smoke-report", "--noop-run", "noop", "--gold-run", "gold"])
    assert (args.noop_run, args.gold_run) == (Path("noop"), Path("gold"))


def test_smoke_report_redacts_windows_paths(tmp_path: Path):
    noop = _smoke_replay(tmp_path, "noop", "noop")
    gold = _smoke_replay(tmp_path, "gold", "gold")
    result_path = gold / "cases/D-O1-full/gold/result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["cleanup"]["message"] = "removed C:/Users/alice/work and D:\\Users\\bob\\work"
    result_path.write_text(json.dumps(result), encoding="utf-8")
    complete_path = result_path.parent / "COMPLETE"
    complete = json.loads(complete_path.read_text(encoding="utf-8"))
    complete["artifacts"]["result.json"] = __import__("hashlib").sha256(result_path.read_bytes()).hexdigest()
    complete_path.write_text(json.dumps(complete), encoding="utf-8")
    paths = generate_smoke_report(noop, gold, destination=tmp_path / "audit/iteration1")
    published = paths.machine_json.read_text(encoding="utf-8")
    assert "alice" not in published and "bob" not in published


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
    assert all(term in text for term in ("https://github.com/PiIIowFighter/ReqCodingAgent", "smoke-report", "validate-all", "WSL2", "Docker"))
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


@pytest.mark.parametrize("name,quote", [("API_KEY", '"'), ("token", "'")])
def test_secret_scan_detects_quoted_credentials(name: str, quote: str):
    value = "sk-proj-" + "abcdefghijklmnop"
    assert _contains_secret(f"{name}={quote}{value}{quote}")


def test_secret_scan_detects_unquoted_punctuation_credentials():
    value = "P@ssw0rd!" + "Production2026"
    assert _contains_secret(f"password={value}")


def test_tracked_text_reads_staged_blob_not_worktree(tmp_path: Path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    path = tmp_path / "config.txt"
    path.write_text("safe\n", encoding="utf-8")
    subprocess.run(["git", "add", "config.txt"], cwd=tmp_path, check=True)
    staged_secret = "API_KEY=" + "abcdefghijklmnop" + "\n"
    path.write_text(staged_secret, encoding="utf-8")
    subprocess.run(["git", "add", "config.txt"], cwd=tmp_path, check=True)
    path.write_text("safe\n", encoding="utf-8")
    assert _tracked_text(tmp_path, "config.txt") == staged_secret


def test_isolation_acceptance_requires_complete_sanitized_proof():
    proof = {
        "status": "passed", "sanitized": True,
        "host_probe": {"positive": True, "negative": True},
        "container_probe": {"positive": True, "negative": True},
        "container_mount_count": 1, "project_root_mounted": False,
        "forbidden_mounts": [], "forbidden_allowlist_entries": [],
        "positive_probe_categories": ["task_repo", "single_public_prompt"],
        "negative_probe_categories": ["benchmark_private", "oracle", "gold_patch", "test_patch", "hints", "plan", "materials", "evaluator_logs", "evaluator_cache", "private_canaries"],
        "prompt_file_sha256": "a" * 64, "workspace_manifest_sha256": "b" * 64,
    }
    assert _is_valid_isolation_proof(proof)
    assert not _is_valid_isolation_proof({**proof, "sanitized": False})
    assert not _is_valid_isolation_proof({**proof, "forbidden_mounts": ["project"]})
    assert not _is_valid_isolation_proof({**proof, "prompt_file_sha256": "short"})


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
    assert not report["criteria"]["django_noop_passed"]
    assert not report["criteria"]["django_gold_passed"]
    assert not report["criteria"]["smoke_report_valid"]


def test_acceptance_maps_every_section19_item_and_never_completes_without_smoke(tmp_path: Path):
    report = evaluate_acceptance(tmp_path, {"status": "failed", "results": []}, unit_tests_passed=True)
    assert set(report["criteria"]) == set(ACCEPTANCE_KEYS)
    assert report["iteration_completion"] is False
    assert report["criteria"]["django_noop_passed"] is False
    assert report["criteria"]["django_gold_passed"] is False
    assert report["criteria"]["smoke_report_valid"] is False
