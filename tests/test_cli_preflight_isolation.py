from __future__ import annotations

import json
from pathlib import Path

from evalsys import cli


def test_cli_preflight_emits_machine_json(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.setattr(cli, "run_preflight", lambda settings: {"schema_version": "1.0", "status": "passed", "bind_mount": {"host_to_container": True}})
    assert cli.main(["--project-root", str(tmp_path), "preflight"]) == 0
    assert json.loads(capsys.readouterr().out)["command"] == "preflight"


def test_cli_isolation_writes_sanitized_proof(monkeypatch, tmp_path: Path, capsys):
    case_path = tmp_path / "case.json"
    case_path.write_text(json.dumps({"case_id": "x", "prompt_variant": "full", "prompt": "public", "prompt_sha256": "a" * 64}), encoding="utf-8")
    task_repo = tmp_path / "task"
    task_repo.mkdir()
    output = tmp_path / "proof.json"
    monkeypatch.setattr(cli, "prove_isolation", lambda *args: {"schema_version": "1.0", "status": "passed", "sanitized": True})
    assert cli.main([
        "--project-root", str(tmp_path), "prove-isolation", "--task-repo", str(task_repo),
        "--public-case", str(case_path), "--workspace", str(tmp_path / "workspace"), "--output", str(output),
    ]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["sanitized"] is True
    assert json.loads(capsys.readouterr().out)["command"] == "prove-isolation"
