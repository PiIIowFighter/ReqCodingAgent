from __future__ import annotations

import json
from pathlib import Path

import pytest

from evalsys.evidence import EvidenceRecorder, sanitize


def test_run_is_non_overwriting_and_contains_required_files(tmp_path: Path):
    recorder = EvidenceRecorder(tmp_path, iteration=1)
    run = recorder.start("unit_tests", {"python": "3.11", "token": "secret"}, ["python", "-m", "pytest"], now="2026-08-28T01:02:03Z")
    run.record_event("started", {"count": 4})
    run.finish({"status": "passed", "passed": 4}, stdout="4 passed", stderr="")
    expected = {"run-manifest.json", "config.snapshot.json", "events.jsonl", "stdout.log", "stderr.log", "result.json", "checksums.sha256", "COMPLETE"}
    assert expected <= {p.name for p in run.raw_dir.iterdir()}
    assert run.audit_dir.is_dir()
    assert {"summary.json", "command.txt", "config-summary.json", "result-summary.json", "log-index.json", "checksums.sha256"} <= {p.name for p in run.audit_dir.iterdir()}
    with pytest.raises(FileExistsError):
        recorder.start("unit_tests", {"python": "3.11", "token": "secret"}, ["python"], now="2026-08-28T01:02:03Z")


def test_index_is_append_only_and_paths_are_relative(tmp_path: Path):
    recorder = EvidenceRecorder(tmp_path, iteration=1)
    first = recorder.start("preflight", {"path": str(tmp_path)}, ["docker", "version"], now="2026-08-28T01:02:03Z")
    first.finish({"status": "passed"})
    second = recorder.start("validate_data", {"revision": "abc"}, ["python"], now="2026-08-28T01:03:03Z")
    second.fail({"status": "failed", "reason": "bad"})
    index = json.loads((tmp_path / "audit/iteration1/index.json").read_text(encoding="utf-8"))
    assert len(index["runs"]) == 2
    assert [entry["status"] for entry in index["runs"]] == ["passed", "failed"]
    assert all(not Path(entry["raw_path"]).is_absolute() for entry in index["runs"])


def test_sanitizer_removes_secrets_and_absolute_paths(tmp_path: Path):
    text = f"API_KEY=abc123 path={tmp_path} ssh-rsa AAAAB3Nza"
    cleaned = sanitize(text, project_root=tmp_path)
    assert "abc123" not in cleaned
    assert str(tmp_path) not in cleaned
    assert "AAAAB3Nza" not in cleaned
