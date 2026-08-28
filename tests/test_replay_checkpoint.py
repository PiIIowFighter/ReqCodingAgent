from __future__ import annotations

import json
from pathlib import Path

import pytest

from evalsys import cli
from evalsys.harness import HarnessInvocation, build_harness_command, extract_test_outcomes
from evalsys.recovery import compute_input_fingerprint, load_reusable_run, write_completed_run
from evalsys.replay import cleanup_run_resources
from evalsys.schema import validate_json
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
    command = build_harness_command(invocation, platform_name="win32")
    assert command[:3] == ["wsl.exe", "-e", "/home/yyt/.local/bin/python3.11"]
    assert command[3].startswith("/mnt/") and command[3].endswith("adapter.py")
    assert "--skip-patch" in command
    assert command[command.index("--harness-checkout") + 1].startswith("/mnt/")
    assert command[command.index("--max-workers") + 1] == "1"


def test_gold_command_uses_official_gold_prediction(tmp_path: Path):
    invocation = HarnessInvocation(tmp_path / "h", tmp_path / "a.py", tmp_path / "d.json", tmp_path / "p.jsonl", tmp_path / "r", "run", "x", "gold", 60)
    command = build_harness_command(invocation, platform_name="linux", python_executable="python3.11")
    assert command[0] == "python3.11"
    assert command[command.index("--predictions") + 1] == "gold"
    assert "--skip-patch" not in command


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


def test_strict_replay_and_event_schemas_reject_unknown_fields():
    validate_json(_result(), "replay-result")
    event = {"schema_version": "1.0", "run_id": "run-1", "sequence": 1, "timestamp": "2026-08-28T00:00:00Z", "event": "stage_started", "stage": "tests", "details": {}}
    validate_json(event, "event")
    event["extra"] = True
    with pytest.raises(Exception):
        validate_json(event, "event")


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


def test_cleanup_filters_current_label_and_never_prunes():
    calls: list[list[str]] = []
    responses = iter(["c1\nc2\n", "n1\n", "v1\n", ""])
    def runner(argv):
        calls.append(list(argv))
        if argv[1:3] == ["ps", "-aq"] or argv[1:3] == ["network", "ls"] or argv[1:3] == ["volume", "ls"] or argv[1:3] == ["image", "ls"]:
            return next(responses)
        return ""
    report = cleanup_run_resources("run-1", runner=runner)
    assert report["containers"] == ["c1", "c2"]
    assert all("label=evalsys.run_id=run-1" in call for call in calls if "--filter" in call)
    assert not any("prune" in call for call in calls)
    assert ["docker", "rm", "-f", "c1", "c2"] in calls


def test_cli_replay_options_and_defaults():
    parser = cli.build_parser()
    args = parser.parse_args(["replay", "--mode", "noop", "--split", "dev"])
    assert (args.timeout, args.workers, args.resume) == (1800, 1, False)
    args = parser.parse_args(["replay", "--mode", "gold", "--split", "all", "--timeout", "5", "--workers", "2", "--resume"])
    assert (args.timeout, args.workers, args.resume) == (5, 2, True)
