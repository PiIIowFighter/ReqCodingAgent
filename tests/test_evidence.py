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
    retry = recorder.start("unit_tests", {"python": "3.11", "token": "secret"}, ["python"], now="2026-08-28T01:02:03Z")
    assert retry.run_id != run.run_id


def test_explicit_run_uses_one_identity_and_existing_raw_root(tmp_path: Path):
    recorder = EvidenceRecorder(tmp_path, iteration=1)
    run_id = "20260828T100000000000Z_replay_noop_abcdef1234"
    raw = recorder.raw_root / run_id
    raw.mkdir(parents=True)
    (raw / "RUNNING").mkdir()
    run = recorder.start_explicit(run_id, "replay_noop", {"split": "dev"}, ["evalsys", "replay"], existing_raw_dir=raw)
    (raw / "case-a").mkdir()
    run.finish({"status": "failed", "passed": 0, "failed": 1})
    index = json.loads((tmp_path / "audit/iteration1/index.json").read_text(encoding="utf-8"))
    assert run.run_id == run_id
    assert run.raw_dir == raw
    assert run.audit_dir == tmp_path / "audit/iteration1/runs" / run_id
    assert [entry["run_id"] for entry in index["runs"]] == [run_id]
    assert not any(path.name != run_id for path in recorder.raw_root.iterdir())
    assert {"run-manifest.json", "config.snapshot.json", "events.jsonl", "stdout.log", "stderr.log", "result.json", "checksums.sha256", "COMPLETE"} <= {path.name for path in raw.iterdir()}


def test_explicit_resume_does_not_add_second_index_entry(tmp_path: Path):
    recorder = EvidenceRecorder(tmp_path, iteration=1)
    run_id = "20260828T100000000000Z_replay_noop_abcdef1234"
    raw = recorder.raw_root / run_id
    raw.mkdir(parents=True)
    first = recorder.start_explicit(run_id, "replay_noop", {}, ["evalsys"], existing_raw_dir=raw)
    first.finish({"status": "failed", "passed": 0, "failed": 1})
    resumed = recorder.start_explicit(run_id, "replay_noop", {}, ["evalsys"], existing_raw_dir=raw, resume=True)
    assert resumed.indexed is True
    resumed.record_event("resume_started", {"attempt": 1})
    resumed.finish({"status": "passed", "passed": 1, "failed": 0})
    attempts = list((raw / "attempts").iterdir())
    assert len(attempts) == 1
    assert {"events.jsonl", "stdout.log", "stderr.log", "result.json", "checksums.sha256", "COMPLETE"} <= {path.name for path in attempts[0].iterdir()}
    index = json.loads((tmp_path / "audit/iteration1/index.json").read_text(encoding="utf-8"))
    assert [entry["run_id"] for entry in index["runs"]] == [run_id]


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
