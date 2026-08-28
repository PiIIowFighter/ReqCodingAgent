from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from evalsys.errors import EvalError
from evalsys.evidence import EvidenceRecorder, verify_checksums
from evalsys.recovery import artifact_hashes_valid, load_reusable_run, write_completed_run
from evalsys.schema import FORMAT_CHECKER, load_json, load_schema, validate_json, validate_jsonl


REPLAY_ARTIFACTS = {
    "stdout.log",
    "stderr.log",
    "events.jsonl",
    "dataset.json",
    "prediction.jsonl",
    "result.json",
    "harness-manifest.json",
}
AUDIT_FILES = {
    "summary.json",
    "command.txt",
    "config-summary.json",
    "result-summary.json",
    "log-index.json",
}


def _result() -> dict:
    return {
        "schema_version": "1.0", "run_id": "run-1", "case_id": "case-1",
        "instance_id": "org__repo-1", "split": "test", "mode": "noop",
        "status": "passed", "classification": "expected_test_statuses",
        "harness_revision": "a" * 40, "data_revision": "b" * 40,
        "repo": "org/repo", "base_commit": "c" * 40, "docker_image": "image",
        "started_at": "2026-08-28T00:00:00Z", "ended_at": "2026-08-28T00:00:01Z",
        "wall_time_s": 1.0,
        "stages": {"environment": "passed", "patch": "skipped", "tests": "passed"},
        "tests_executed": True, "fail_to_pass": {}, "pass_to_pass": {},
        "logs": {"stdout": "stdout.log", "stderr": "stderr.log", "harness": "harness"},
        "error": None, "cleanup": {"status": "passed", "message": None},
    }


def _replay_files(root: Path) -> None:
    for name in REPLAY_ARTIFACTS - {"result.json", "harness-manifest.json"}:
        (root / name).write_text("fixture\n", encoding="utf-8")
    (root / "harness").mkdir()
    (root / "harness" / "output.log").write_text("ran\n", encoding="utf-8")


def _write_audit_checksums(root: Path, *, names: set[str] = AUDIT_FILES) -> None:
    root.mkdir(parents=True, exist_ok=True)
    lines = []
    for name in sorted(names):
        payload = (name + "\n").encode()
        (root / name).write_bytes(payload)
        lines.append(f"{hashlib.sha256(payload).hexdigest()}  {name}")
    (root / "checksums.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.mark.parametrize("name", ["", "../outside", "/absolute", "C:/absolute", "a\\b", "a//b", "./a", "a/../b"])
def test_artifact_hashes_reject_unsafe_noncanonical_names(tmp_path: Path, name: str):
    outside = tmp_path.parent / "outside"
    outside.write_text("attacker", encoding="utf-8")
    digest = hashlib.sha256(b"attacker").hexdigest()
    assert artifact_hashes_valid(tmp_path, {name: digest}) is False


def test_artifact_hashes_reject_matching_hash_through_symlink(tmp_path: Path):
    outside = tmp_path.parent / "outside-target"
    outside.write_text("attacker", encoding="utf-8")
    try:
        (tmp_path / "linked").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation unavailable")
    assert not artifact_hashes_valid(tmp_path, {"linked": hashlib.sha256(b"attacker").hexdigest()})


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction behavior")
def test_artifact_hashes_reject_windows_junction_chain(tmp_path: Path):
    outside = tmp_path.parent / "junction-target"
    outside.mkdir()
    (outside / "payload").write_text("attacker", encoding="utf-8")
    junction = tmp_path / "junction"
    if subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(junction), str(outside)], capture_output=True).returncode:
        pytest.skip("junction creation unavailable")
    digest = hashlib.sha256(b"attacker").hexdigest()
    assert not artifact_hashes_valid(tmp_path, {"junction/payload": digest})


def test_completed_replay_requires_exact_artifacts_and_harness_root(tmp_path: Path):
    _replay_files(tmp_path)
    marker = write_completed_run(
        tmp_path, _result(), "f" * 64,
        ["stdout.log", "stderr.log", "events.jsonl", "dataset.json", "prediction.jsonl"],
        artifact_trees=["harness"],
    )
    assert set(marker["artifacts"]) == REPLAY_ARTIFACTS
    assert load_reusable_run(tmp_path, "f" * 64) == _result()

    marker["artifacts"].pop("stdout.log")
    (tmp_path / "COMPLETE").write_text(json.dumps(marker), encoding="utf-8")
    assert load_reusable_run(tmp_path, "f" * 64) is None


@pytest.mark.parametrize("missing", ["result.json", "stdout.log", "harness-manifest.json"])
def test_completed_replay_rejects_missing_required_artifact(tmp_path: Path, missing: str):
    _replay_files(tmp_path)
    write_completed_run(tmp_path, _result(), "f" * 64,
                        ["stdout.log", "stderr.log", "events.jsonl", "dataset.json", "prediction.jsonl"],
                        artifact_trees=["harness"])
    (tmp_path / missing).unlink()
    assert load_reusable_run(tmp_path, "f" * 64) is None


def test_completed_replay_rejects_nonfinite_result(tmp_path: Path):
    _replay_files(tmp_path)
    write_completed_run(tmp_path, _result(), "f" * 64,
                        ["stdout.log", "stderr.log", "events.jsonl", "dataset.json", "prediction.jsonl"],
                        artifact_trees=["harness"])
    result_path = tmp_path / "result.json"
    payload = result_path.read_text(encoding="utf-8").replace('"wall_time_s": 1.0', '"wall_time_s": NaN')
    result_path.write_text(payload, encoding="utf-8")
    marker_path = tmp_path / "COMPLETE"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["artifacts"]["result.json"] = hashlib.sha256(result_path.read_bytes()).hexdigest()
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    assert load_reusable_run(tmp_path, "f" * 64) is None


def test_completed_replay_rejects_duplicate_json_keys(tmp_path: Path):
    _replay_files(tmp_path)
    write_completed_run(tmp_path, _result(), "f" * 64,
                        ["stdout.log", "stderr.log", "events.jsonl", "dataset.json", "prediction.jsonl"],
                        artifact_trees=["harness"])
    marker_path = tmp_path / "COMPLETE"
    marker = marker_path.read_text(encoding="utf-8")
    marker_path.write_text(marker.replace('"schema_version": "1.0"', '"schema_version": "1.0",\n  "schema_version": "1.0"', 1), encoding="utf-8")
    assert load_reusable_run(tmp_path, "f" * 64) is None


def test_completed_replay_rejects_substitute_harness_roots(tmp_path: Path):
    _replay_files(tmp_path)
    write_completed_run(tmp_path, _result(), "f" * 64,
                        ["stdout.log", "stderr.log", "events.jsonl", "dataset.json", "prediction.jsonl"],
                        artifact_trees=["harness"])
    manifest_path = tmp_path / "harness-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["roots"] = []
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    marker_path = tmp_path / "COMPLETE"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["artifacts"]["harness-manifest.json"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    assert load_reusable_run(tmp_path, "f" * 64) is None


@pytest.mark.parametrize("bad_line", [
    "", "a" * 63 + "  summary.json\n", "A" * 64 + "  summary.json\n",
    "a" * 64 + " summary.json\n", "a" * 64 + "   summary.json\n",
])
def test_verify_checksums_rejects_empty_or_malformed_manifest(tmp_path: Path, bad_line: str):
    (tmp_path / "checksums.sha256").write_text(bad_line, encoding="utf-8")
    with pytest.raises(ValueError):
        verify_checksums(tmp_path)


def test_verify_checksums_reports_missing_required_file(tmp_path: Path):
    _write_audit_checksums(tmp_path)
    (tmp_path / "summary.json").unlink()
    assert verify_checksums(tmp_path) == ["summary.json"]


def test_verify_checksums_rejects_symlinked_audit_file(tmp_path: Path):
    _write_audit_checksums(tmp_path)
    outside = tmp_path.parent / "outside-audit"
    outside.write_text("summary.json\n", encoding="utf-8")
    (tmp_path / "summary.json").unlink()
    try:
        (tmp_path / "summary.json").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(ValueError, match="symlink|reparse|escapes root"):
        verify_checksums(tmp_path)


def test_verify_checksums_requires_exact_audit_names_and_no_duplicates(tmp_path: Path):
    _write_audit_checksums(tmp_path, names={"summary.json"})
    with pytest.raises(ValueError, match="exactly"):
        verify_checksums(tmp_path)
    _write_audit_checksums(tmp_path)
    line = (tmp_path / "checksums.sha256").read_text(encoding="utf-8").splitlines()[0]
    with (tmp_path / "checksums.sha256").open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")
    with pytest.raises(ValueError, match="duplicate"):
        verify_checksums(tmp_path)


@pytest.mark.parametrize("payload", ['{"a": 1, "a": 2}', '{"x": NaN}', '{"x": Infinity}', '{"x": -Infinity}'])
def test_strict_json_loader_rejects_duplicates_and_nonfinite_numbers(tmp_path: Path, payload: str):
    path = tmp_path / "value.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(EvalError):
        load_json(path)


def test_jsonl_loader_is_strict(tmp_path: Path):
    path = tmp_path / "cases.jsonl"
    path.write_text('{"case_id":"x","case_id":"y"}\n', encoding="utf-8")
    with pytest.raises(EvalError, match="line 1"):
        validate_jsonl(path, "public-case")


def test_schema_loader_rejects_traversal_and_invalid_names():
    for name in ("../source-lock", "source_lock", "Source-lock", "", "a/b"):
        with pytest.raises(EvalError, match="schema name"):
            load_schema(name)


def test_required_format_checkers_are_registered_locally():
    assert {"date-time", "uri"} <= set(FORMAT_CHECKER.checkers)


@pytest.mark.parametrize("value", [
    "not-a-date", "2026-08-28T00:00:00", "2026-08-28 00:00:00Z",
    "2026-08-28T00:00:00", "2026-02-30T00:00:00Z", "2026-08-28T24:00:00Z",
])
def test_datetime_checker_rejects_invalid_or_naive_values(value: str):
    result = _result()
    result["started_at"] = value
    with pytest.raises(EvalError, match="date-time"):
        validate_json(result, "replay-result")


def test_datetime_checker_accepts_z_and_explicit_offsets():
    for value in ("2026-08-28T00:00:00Z", "2026-08-28T00:00:00.123456Z", "2026-08-28T08:00:00+08:00"):
        result = _result()
        result["started_at"] = value
        validate_json(result, "replay-result")


@pytest.mark.parametrize("value", ["not a uri", "https:///missing-host", "/relative", "example.com/path"])
def test_uri_checker_rejects_missing_scheme_or_authority(value: str):
    source = {"url": "https://example.invalid/repo", "revision": "a" * 40}
    lock = {"schema_version": "1.0", "sources": {"harness": {**source, "url": value}, "verified": source, "lite": source}}
    with pytest.raises(EvalError, match="uri|https"):
        validate_json(lock, "source-lock")


def test_schema_format_checker_rejects_invalid_datetime_and_uri():
    for field in ("started_at", "ended_at"):
        result = _result()
        result[field] = "not-a-date"
        with pytest.raises(EvalError, match="date-time"):
            validate_json(result, "replay-result")
    source = {"url": "https://example.invalid/repo", "revision": "a" * 40}
    lock = {"schema_version": "1.0", "sources": {"harness": {**source, "url": "not a uri"}, "verified": source, "lite": source}}
    with pytest.raises(EvalError, match="uri"):
        validate_json(lock, "source-lock")


def test_source_lock_schema_accepts_real_https_values_and_rejects_non_https():
    lock = load_json(Path(__file__).parents[1] / "benchmark" / "source-lock.json")
    validate_json(lock, "source-lock")
    lock["sources"]["harness"]["url"] = "ftp://example.invalid/repo"
    with pytest.raises(EvalError, match="https"):
        validate_json(lock, "source-lock")


def test_all_timestamp_and_url_schema_fields_declare_formats():
    replay = load_schema("replay-result")["properties"]
    event = load_schema("event")["properties"]
    source = load_schema("source-lock")["$defs"]["source"]["properties"]
    assert replay["started_at"]["format"] == "date-time"
    assert replay["ended_at"]["format"] == "date-time"
    assert event["timestamp"]["format"] == "date-time"
    assert source["url"]["format"] == "uri"


@pytest.mark.parametrize("field,value", [
    ("stdout", "../stdout.log"), ("stderr", "/tmp/stderr.log"),
    ("harness", "harness\\logs"), ("harness", "harness/../outside"),
    ("stdout", "stdout.log\n"),
])
def test_replay_schema_rejects_traversal_in_log_paths(field: str, value: str):
    result = _result()
    result["logs"][field] = value
    with pytest.raises(EvalError):
        validate_json(result, "replay-result")


def test_evidence_start_rejects_unsafe_run_type_and_timestamp(tmp_path: Path):
    recorder = EvidenceRecorder(tmp_path, iteration=1)
    for run_type in ("../unit", "unit/test", "unit\\test", "Unit Tests", ""):
        with pytest.raises(ValueError, match="run_type"):
            recorder.start(run_type, {}, ["test"])
    for now in ("../x", "2026-08-28", "2026-08-28T01:02:03+00:00", "2026-08-28T01:02:03Z/../x"):
        with pytest.raises(ValueError, match="timestamp"):
            recorder.start("unit_tests", {}, ["test"], now=now)


def test_evidence_start_same_time_is_collision_safe(tmp_path: Path):
    recorder = EvidenceRecorder(tmp_path, iteration=1)
    first = recorder.start("unit_tests", {}, ["test"], now="2026-08-28T01:02:03.123456Z")
    second = recorder.start("unit_tests", {}, ["test"], now="2026-08-28T01:02:03.123456Z")
    assert first.run_id != second.run_id
    assert first.raw_dir.is_dir() and second.raw_dir.is_dir()


def test_atomic_evidence_rejects_nan_before_terminal_artifacts(tmp_path: Path):
    run = EvidenceRecorder(tmp_path, iteration=1).start("unit_tests", {}, ["test"])
    with pytest.raises(ValueError):
        run.finish({"status": "passed", "duration": float("nan")})
    assert not (run.raw_dir / "result.json").exists()
    assert not (run.raw_dir / "COMPLETE").exists()
