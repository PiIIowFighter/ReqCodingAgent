from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from evalsys.evidence import (
    EvidenceRecorder,
    select_current_runs,
    verify_active_audit_runs,
    verify_checksums,
)


def test_generated_text_uses_utf8_lf_and_survives_git_normalization(tmp_path: Path):
    recorder = EvidenceRecorder(tmp_path, iteration=1)
    run = recorder.start("unit_tests", {"label": "中文"}, ["python", "-m", "pytest"], now="2026-08-28T03:00:00Z")
    run.finish({"status": "passed", "passed": 7, "failed": 0, "skipped": 0, "duration": 0.29, "exit_code": 0}, stdout="7 passed\r\n", stderr="")
    for path in list(run.raw_dir.iterdir()) + list(run.audit_dir.iterdir()):
        if path.is_file():
            assert b"\r\n" not in path.read_bytes(), path
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "core.autocrlf", "true"], cwd=tmp_path, check=True)
    (tmp_path / ".gitattributes").write_bytes(b"audit/** text eol=lf\n*.json text eol=lf\n*.sha256 text eol=lf\n*.txt text eol=lf\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    assert verify_checksums(run.audit_dir) == []
    for line in (run.audit_dir / "checksums.sha256").read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        blob = subprocess.check_output(["git", "show", f":audit/iteration1/runs/{run.run_id}/{name}"], cwd=tmp_path)
        assert hashlib.sha256(blob).hexdigest() == digest


def test_public_evidence_contains_counts_and_raw_file_metadata(tmp_path: Path):
    run = EvidenceRecorder(tmp_path, iteration=1).start("unit_tests", {}, ["pytest"], now="2026-08-28T03:01:00Z")
    run.finish({"status": "passed", "passed": 7, "failed": 0, "skipped": 1, "duration": 1.25, "exit_code": 0}, stdout="7 passed, 1 skipped\n")
    result = json.loads((run.audit_dir / "result-summary.json").read_text(encoding="utf-8"))
    assert result == {"status": "passed", "passed": 7, "failed": 0, "skipped": 1, "duration": 1.25, "exit_code": 0}
    logs = json.loads((run.audit_dir / "log-index.json").read_text(encoding="utf-8"))
    assert set(logs) == {"stdout.log", "stderr.log", "result.json", "checksums.sha256"}
    assert all(set(value) == {"sha256", "bytes"} for value in logs.values())
    assert logs["stdout.log"]["bytes"] == len(b"7 passed, 1 skipped\n")


def test_checkout_verifier_skips_preserved_invalid_legacy_runs(tmp_path: Path):
    audit = tmp_path / "audit/iteration1"
    old_dir = audit / "runs/old"
    old_dir.mkdir(parents=True)
    (old_dir / "summary.json").write_bytes(b"legacy\r\n")
    (old_dir / "checksums.sha256").write_text(f"{'0' * 64}  summary.json\n", encoding="utf-8")
    active_dir = audit / "runs/new"
    active_dir.mkdir(parents=True)
    (active_dir / "summary.json").write_bytes(b"new\n")
    digest = hashlib.sha256(b"new\n").hexdigest()
    (active_dir / "checksums.sha256").write_text(f"{digest}  summary.json\n", encoding="utf-8")
    (audit / "index.json").write_text(json.dumps({"runs": [
        {"run_id": "old", "validity": "invalid", "audit_path": "audit/iteration1/runs/old"},
        {"run_id": "new", "validity": "active", "audit_path": "audit/iteration1/runs/new"},
    ]}), encoding="utf-8")
    assert verify_active_audit_runs(tmp_path, iteration=1) == {"new": []}


def test_current_runs_are_active_supersedes_leaves_not_all_active_runs():
    runs = [
        {"run_id": "root", "run_type": "unit_tests", "validity": "active", "supersedes": []},
        {"run_id": "branch-a", "run_type": "unit_tests", "validity": "active", "supersedes": ["root"]},
        {"run_id": "leaf-a", "run_type": "unit_tests", "validity": "active", "supersedes": ["branch-a"]},
        {"run_id": "leaf-b", "run_type": "unit_tests", "validity": "active", "supersedes": ["root"]},
        {"run_id": "invalid-leaf", "run_type": "unit_tests", "validity": "invalid", "supersedes": ["leaf-b"]},
        {"run_id": "preflight", "run_type": "preflight", "validity": "active", "supersedes": []},
    ]
    assert [run["run_id"] for run in select_current_runs(runs)] == ["leaf-a", "leaf-b", "preflight"]


@pytest.mark.parametrize("runs,match", [
    ([{"run_id": "a", "run_type": "unit_tests", "validity": "active", "supersedes": ["missing"]}], "unknown"),
    ([{"run_id": "a", "run_type": "unit_tests", "validity": "active", "supersedes": ["b"]}, {"run_id": "b", "run_type": "unit_tests", "validity": "active", "supersedes": ["a"]}], "cycle"),
    ([{"run_id": "a", "run_type": "unit_tests", "validity": "active", "supersedes": []}, {"run_id": "b", "run_type": "preflight", "validity": "active", "supersedes": ["a"]}], "run_type"),
    ([{"run_id": "a", "run_type": "unit_tests", "validity": "active", "supersedes": []}, {"run_id": "a", "run_type": "unit_tests", "validity": "active", "supersedes": []}], "duplicate"),
])
def test_current_run_graph_rejects_invalid_dag_type_and_scope(runs, match):
    import pytest
    with pytest.raises(ValueError, match=match):
        select_current_runs(runs)


def test_active_means_checksum_valid_not_current_result():
    runs = [
        {"run_id": "old", "run_type": "unit_tests", "validity": "active", "supersedes": []},
        {"run_id": "new", "run_type": "unit_tests", "validity": "active", "supersedes": ["old"]},
    ]
    assert all(run["validity"] == "active" for run in runs)
    assert [run["run_id"] for run in select_current_runs(runs)] == ["new"]


def test_failed_summary_includes_sanitized_bounded_classification_and_reason(tmp_path: Path):
    run = EvidenceRecorder(tmp_path, iteration=1).start("preflight", {}, ["check"], now="2026-08-28T03:03:00Z")
    secret = "API_KEY=top-secret " + str(tmp_path) + " " + ("x" * 9000)
    run.fail({"status": "infra_failed", "passed": 0, "failed": 1, "skipped": 0, "duration": 0.3, "exit_code": 2, "classification": "docker_unavailable", "reason": secret})
    summary = json.loads((run.audit_dir / "result-summary.json").read_text(encoding="utf-8"))
    assert summary["classification"] == "docker_unavailable"
    assert "top-secret" not in summary["reason"]
    assert str(tmp_path) not in summary["reason"]
    assert len(summary["reason"]) <= 8192


def test_superseding_run_records_relationship(tmp_path: Path):
    old = "20260828T010000Z_unit_tests_aaaaaaaaaa"
    audit = tmp_path / "audit/iteration1"
    audit.mkdir(parents=True)
    (audit / "index.json").write_text(json.dumps({"schema_version": "1.0", "iteration": 1, "runs": [{"run_id": old, "status": "passed", "validity": "active"}]}), encoding="utf-8")
    recorder = EvidenceRecorder(tmp_path, iteration=1)
    recorder.invalidate_runs([old], reason="Windows CRLF was normalized to LF by Git, invalidating checksums")
    run = recorder.start("unit_tests", {}, ["pytest"], now="2026-08-28T03:02:00Z", supersedes=[old])
    run.finish({"status": "passed", "passed": 7, "failed": 0, "skipped": 0, "duration": 0.2, "exit_code": 0})
    index = json.loads((audit / "index.json").read_text(encoding="utf-8"))
    assert index["runs"][0]["validity"] == "invalid"
    assert "CRLF" in index["runs"][0]["invalid_reason"]
    assert index["runs"][1]["supersedes"] == [old]
