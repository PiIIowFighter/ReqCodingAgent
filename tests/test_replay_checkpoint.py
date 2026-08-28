from __future__ import annotations

import json
from pathlib import Path

import pytest

from evalsys import cli
from evalsys.config import Settings
from evalsys.domain import REPLAY_STATUSES
from evalsys.harness import HarnessInvocation, build_harness_command, extract_test_outcomes
from evalsys.preflight import CommandRunner, resolve_wsl_path, validate_wsl_python
from evalsys.recovery import compute_input_fingerprint, load_reusable_run, write_completed_run
from evalsys.replay import cleanup_run_resources, create_run_directory
from evalsys.schema import validate_json
from scripts.official_harness_adapter import classify_artifacts
from evalsys.verdict import decide_verdict


def _result(status: str = "passed") -> dict:
    return {
        "schema_version": "1.0",
        "run_id": "run-1",
        "case_id": "T-O1-full",
        "instance_id": "astropy__astropy-14995",
        "split": "test",
        "mode": "noop",
        "status": status,
        "classification": "expected_test_statuses",
        "harness_revision": "a" * 40,
        "data_revision": "b" * 40,
        "repo": "astropy/astropy",
        "base_commit": "c" * 40,
        "docker_image": "swebench/sweb.eval.x:latest",
        "started_at": "2026-08-28T00:00:00Z",
        "ended_at": "2026-08-28T00:00:01Z",
        "wall_time_s": 1.0,
        "stages": {"environment": "passed", "patch": "skipped", "tests": "passed"},
        "tests_executed": True,
        "fail_to_pass": {"test_a": "FAILED"},
        "pass_to_pass": {"test_b": "PASSED"},
        "logs": {"stdout": "stdout.log", "stderr": "stderr.log", "harness": "harness"},
        "error": None,
    }


class WslRunner(CommandRunner):
    def __init__(self):
        self.calls = []
    def run(self, argv, *, timeout_s=30):
        import subprocess
        self.calls.append(list(argv))
        if "wslpath" in argv:
            return subprocess.CompletedProcess(argv, 0, "/custom/挂载/path\n", "")
        return subprocess.CompletedProcess(argv, 0, "Python 3.11.16\n", "")


def test_settings_configures_wsl_python_without_username(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("EVALSYS_WSL_PYTHON", "/external/venv/bin/python")
    settings = Settings.from_env(tmp_path)
    assert settings.wsl_python == "/external/venv/bin/python"
    assert settings.docker_prefix("win32") == ["wsl.exe", "--", "docker"]


def test_preflight_path_and_python_are_shared_argument_array_helpers(tmp_path: Path):
    runner = WslRunner()
    assert resolve_wsl_path(tmp_path / "中文 空格", runner) == "/custom/挂载/path"
    assert validate_wsl_python("python3.11", runner) == "python3.11"
    assert ["wsl.exe", "--", "wslpath", "-a", (tmp_path / "中文 空格").as_posix()] in runner.calls
    assert ["wsl.exe", "--", "python3.11", "--version"] in runner.calls


def test_official_command_uses_wsl_python311_arrays_and_skip_patch(tmp_path: Path):
    invocation = HarnessInvocation(
        harness_checkout=tmp_path / "fixed harness",
        adapter_path=tmp_path / "adapter.py",
        dataset_path=tmp_path / "task.json",
        predictions_path=tmp_path / "prediction.jsonl",
        report_dir=tmp_path / "report dir",
        run_id="run-1",
        instance_id="x__x-1",
        mode="noop",
        timeout_s=123,
    )
    command = build_harness_command(invocation, platform_name="win32", wsl_python="/external/venv/bin/python", path_converter=lambda path: "/translated/" + path.name)
    assert command[:3] == ["wsl.exe", "--", "/external/venv/bin/python"]
    assert command[3] == "/translated/adapter.py"
    assert "--skip-patch" in command
    assert command[command.index("--harness-checkout") + 1] == "/translated/fixed harness"
    assert command[command.index("--max-workers") + 1] == "1"


def test_gold_command_uses_official_gold_prediction(tmp_path: Path):
    invocation = HarnessInvocation(tmp_path / "h", tmp_path / "a.py", tmp_path / "d.json", tmp_path / "p.jsonl", tmp_path / "r", "run", "x", "gold", 60)
    command = build_harness_command(invocation, platform_name="linux", python_executable="python3.11")
    assert command[0] == "python3.11"
    assert command[command.index("--predictions") + 1] == "gold"
    assert "--skip-patch" not in command


@pytest.mark.parametrize("marker,status,classification", [
    (">>>>> Tests Timed Out", "timeout", "official_tests_timeout"),
    ("Error in evaluating model: Docker image unavailable", "infra_failed", "official_environment_failure"),
    (">>>>> Patch Apply Failed", "invalid", "official_patch_apply_failure"),
])
def test_adapter_classifies_exit_zero_official_failure_markers(tmp_path: Path, marker: str, status: str, classification: str):
    log_dir = tmp_path / "model" / "instance"
    log_dir.mkdir(parents=True)
    (log_dir / "run_instance.log").write_text(marker, encoding="utf-8")
    result = classify_artifacts(tmp_path, "instance", skip_patch=False)
    assert (result["status"], result["classification"]) == (status, classification)


def test_adapter_classifies_missing_real_output(tmp_path: Path):
    assert classify_artifacts(tmp_path, "instance", skip_patch=True)["classification"] == "missing_instance_log"


def test_extract_requires_execution_marker_and_every_exact_status():
    raw = {"tests_status": {"FAIL_TO_PASS": {"success": [], "failure": ["a"]}, "PASS_TO_PASS": {"success": ["b"], "failure": []}}}
    parsed = extract_test_outcomes(raw, ["a"], ["b"], tests_executed=True)
    assert parsed == {"a": "FAILED", "b": "PASSED"}
    with pytest.raises(ValueError, match="execution marker"):
        extract_test_outcomes(raw, ["a"], ["b"], tests_executed=False)


@pytest.mark.parametrize("mutation,match", [
    (lambda r: r["tests_status"]["FAIL_TO_PASS"]["success"].append("a"), "duplicate"),
    (lambda r: r["tests_status"]["PASS_TO_PASS"]["success"].clear(), "missing"),
    (lambda r: r["tests_status"]["PASS_TO_PASS"].update({"mystery": ["b"]}), "unknown"),
])
def test_extract_rejects_duplicate_missing_and_unknown(mutation, match):
    raw = {"tests_status": {"FAIL_TO_PASS": {"success": [], "failure": ["a"]}, "PASS_TO_PASS": {"success": ["b"], "failure": []}}}
    mutation(raw)
    with pytest.raises(ValueError, match=match):
        extract_test_outcomes(raw, ["a"], ["b"], tests_executed=True)


def test_extract_accepts_raw_official_parser_status_map():
    assert extract_test_outcomes({"outcomes": {"a": "FAILED", "b": "PASSED"}}, ["a"], ["b"], tests_executed=True) == {"a": "FAILED", "b": "PASSED"}


def test_invalid_mode_has_explicit_classification():
    assert decide_verdict("prediction", {"a": "PASSED"}, ["a"], [])["classification"] == "invalid_mode"


def test_domain_statuses_match_all_output_schemas():
    root = Path(__file__).parents[1] / "benchmark" / "schemas"
    replay = json.loads((root / "replay-result.schema.json").read_text(encoding="utf-8"))
    summary = json.loads((root / "validation-summary.schema.json").read_text(encoding="utf-8"))
    assert set(replay["properties"]["status"]["enum"]) == REPLAY_STATUSES
    assert set(summary["properties"]["results"]["items"]["properties"]["status"]["enum"]) == REPLAY_STATUSES


def test_strict_replay_and_event_schemas_reject_unknown_fields():
    validate_json(_result(), "replay-result")
    event = {"schema_version": "1.0", "run_id": "run-1", "sequence": 1, "timestamp": "2026-08-28T00:00:00Z", "event": "stage_started", "stage": "tests", "details": {}}
    validate_json(event, "event")
    event["extra"] = True
    with pytest.raises(Exception):
        validate_json(event, "event")


def test_recovery_hashes_harness_tree_manifest(tmp_path: Path):
    harness = tmp_path / "harness"
    (harness / "logs").mkdir(parents=True)
    (harness / "adapter-result.json").write_text("{}", encoding="utf-8")
    (harness / "logs" / "test_output.txt").write_text("ran", encoding="utf-8")
    for name in ("stdout.log", "stderr.log", "events.jsonl"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    fp = compute_input_fingerprint({"x": 1})
    write_completed_run(tmp_path, _result(), fp, ["stdout.log", "stderr.log", "events.jsonl"], artifact_trees=["harness"])
    manifest = json.loads((tmp_path / "harness-manifest.json").read_text(encoding="utf-8"))
    assert [entry["path"] for entry in manifest["files"]] == ["harness/adapter-result.json", "harness/logs/test_output.txt"]
    assert load_reusable_run(tmp_path, fp) is not None
    (harness / "logs" / "test_output.txt").unlink()
    assert load_reusable_run(tmp_path, fp) is None


def test_recovery_reuses_only_complete_schema_valid_hashed_artifacts(tmp_path: Path):
    fingerprint = compute_input_fingerprint({"instance_id": "x", "mode": "noop", "tests": ["a"]})
    for name in ("stdout.log", "stderr.log", "events.jsonl", "harness.json"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    write_completed_run(tmp_path, _result(), fingerprint, ["stdout.log", "stderr.log", "events.jsonl", "harness.json"])
    assert load_reusable_run(tmp_path, fingerprint) == _result()
    (tmp_path / "stdout.log").write_text("corrupt", encoding="utf-8")
    assert load_reusable_run(tmp_path, fingerprint) is None


@pytest.mark.parametrize("breakage", ["marker", "result", "temporary", "nonterminal"])
def test_recovery_rejects_incomplete_or_invalid_runs(tmp_path: Path, breakage: str):
    fp = compute_input_fingerprint({"x": 1})
    for name in ("stdout.log", "stderr.log", "events.jsonl"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    result = _result("infra_failed" if breakage != "nonterminal" else "running")
    if breakage == "nonterminal":
        (tmp_path / "result.json").write_text(json.dumps(result), encoding="utf-8")
        (tmp_path / "COMPLETE").write_text(json.dumps({"schema_version": "1.0", "input_fingerprint": fp, "result": "result.json", "artifacts": {}}), encoding="utf-8")
    else:
        write_completed_run(tmp_path, result, fp, ["stdout.log", "stderr.log", "events.jsonl"])
    if breakage == "marker":
        (tmp_path / "COMPLETE").unlink()
    elif breakage == "result":
        (tmp_path / "result.json").write_text("{}", encoding="utf-8")
    elif breakage == "temporary":
        (tmp_path / "result.json.tmp").write_text("partial", encoding="utf-8")
    assert load_reusable_run(tmp_path, fp) is None


def test_run_directory_is_atomic_and_resume_reuses_explicit_id(tmp_path: Path):
    first = create_run_directory(tmp_path, "run-1", resume=False)
    assert first == tmp_path / "run-1"
    with pytest.raises(FileExistsError):
        create_run_directory(tmp_path, "run-1", resume=False)
    lease = first / "RUNNING"
    lease.write_text("occupied", encoding="utf-8")
    with pytest.raises(RuntimeError, match="occupied"):
        create_run_directory(tmp_path, "run-1", resume=True)
    lease.unlink()
    assert create_run_directory(tmp_path, "run-1", resume=True) == first


def test_cleanup_filters_current_label_and_never_prunes():
    calls: list[list[str]] = []
    responses = iter(["c1\nc2\n", "n1\n", "v1\n", ""])
    def runner(argv):
        calls.append(list(argv))
        if argv[3:5] == ["ps", "-aq"] or argv[3:5] == ["network", "ls"] or argv[3:5] == ["volume", "ls"] or argv[3:5] == ["image", "ls"]:
            return next(responses)
        return ""
    report = cleanup_run_resources("run-1", case_id="case-1", runner=runner, docker_prefix=["wsl.exe", "--", "docker"])
    assert report["containers"] == ["c1", "c2"]
    assert all("label=evalsys.run_id=run-1" in call and "label=evalsys.case_id=case-1" in call for call in calls if "--filter" in call)
    assert not any("prune" in call for call in calls)
    assert ["wsl.exe", "--", "docker", "rm", "-f", "c1", "c2"] in calls


def test_cli_replay_options_and_defaults():
    parser = cli.build_parser()
    args = parser.parse_args(["replay", "--mode", "noop", "--split", "dev"])
    assert (args.timeout, args.workers, args.resume, args.run_id) == (1800, 1, False, None)
    args = parser.parse_args(["replay", "--mode", "gold", "--split", "all", "--timeout", "5", "--workers", "2", "--resume", "--run-id", "existing"])
    assert (args.timeout, args.workers, args.resume, args.run_id) == (5, 2, True, "existing")
