from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from evalsys.agent_runner import AgentRunRequest, preflight_agent_config
from evalsys.baseline import require_frozen_baseline
from evalsys.errors import EvalError
from reqagent.checkpoint import CheckpointStore
from reqagent.config import AgentConfig
from reqagent.context import ContextLedger
from reqagent.model import ModelMessage, ModelRequest, ScriptedModel
from reqagent.trace import RunStore
from reqagent.workspace import GitWorkspace


ROOT = Path(__file__).resolve().parents[1]


def test_context_compaction_preserves_system_task_and_recent_rounds():
    ledger = ContextLedger("system", "task", context_window=40, trigger_ratio=.5, keep_recent_rounds=2)
    for index in range(8):
        ledger.add(ModelMessage("assistant" if index % 2 == 0 else "tool", "x" * 100))
    assert ledger.compact_if_needed([], "abc")
    assert ledger.messages[0].text == "system"
    assert ledger.messages[1].text == "task"
    assert ledger.messages[2].text.startswith("Earlier interaction summary:")
    assert len(ledger.messages) == 7


def test_resume_checkpoint_success_and_workspace_mismatch_refusal(tmp_path: Path):
    run = tmp_path / "run"
    run.mkdir()
    checkpoint = CheckpointStore(run)
    payload = {"base_commit": "abc", "diff_hash": "clean", "config_hash": "cfg", "budgets": {"max_steps": 3}}
    checkpoint.save(1, payload)
    assert checkpoint.load()["diff_hash"] == "clean"


def test_cli_resume_continues_incomplete_run_and_rejects_changed_workspace(tmp_path: Path):
    source = tmp_path / "repo"
    source.mkdir()
    subprocess.run(["git", "-C", str(source), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
    (source / "hello.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "hello.py"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "initial"], check=True)
    config = json.loads((ROOT / "configs/agent/offline-scripted.json").read_text(encoding="utf-8"))
    config["script"] = [
        {"text": "", "tool_calls": [{"call_id": "1", "name": "read_file", "arguments": {"path": "hello.py"}}], "usage": {}, "finish_reason": "tool_calls", "provider_request_id": "1"},
        {"text": "", "tool_calls": [{"call_id": "2", "name": "submit", "arguments": {"summary": "done", "tests": [], "limitations": ""}}], "usage": {}, "finish_reason": "tool_calls", "provider_request_id": "2"},
    ]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    workspace = GitWorkspace.create(source)
    artifact_root = tmp_path / "runs"
    store = RunStore.create(artifact_root)
    system = "system"
    ledger = ContextLedger(system, "task", context_window=10000, trigger_ratio=.8, keep_recent_rounds=2)
    model = ScriptedModel(config["script"])
    response = model.complete(ModelRequest(tuple(ledger.messages), (), 100, 10))
    ledger.add(ModelMessage("assistant", response.text, response.tool_calls))
    manifest = {"run_id": store.run_id, "source": str(source), "workspace": str(workspace.root), "task": "task", "base_commit": workspace.base_commit, "config_path": str(config_path)}
    (store.path / "run-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    loaded = AgentConfig.load(config_path)
    CheckpointStore(store.path).save(1, {"run_id": store.run_id, "next_state": "call_model", "steps": 1, "tool_calls": 0, "invalid_outputs": 0, "usage": {}, "messages": [message.to_dict() for message in ledger.messages], "context_window": 10000, "config_hash": loaded.canonical_hash(), "base_commit": workspace.base_commit, "diff_hash": workspace.diff_hash(), "script_position": 1, "budgets": loaded.budgets, "task_hash": "unused"})
    command = [sys.executable, "-m", "reqagent.cli", "resume", "--run-id", store.run_id, "--artifact-root", str(artifact_root), "--config", str(config_path)]
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    completed = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
    assert completed.returncode == 0
    assert json.loads(completed.stdout)["stop_reason"] == "submitted"

    refusal = RunStore.create(artifact_root)
    manifest["run_id"] = refusal.run_id
    (refusal.path / "run-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    CheckpointStore(refusal.path).save(1, {"run_id": refusal.run_id, "next_state": "call_model", "steps": 1, "tool_calls": 0, "invalid_outputs": 0, "usage": {}, "messages": [message.to_dict() for message in ledger.messages], "context_window": 10000, "config_hash": loaded.canonical_hash(), "base_commit": workspace.base_commit, "diff_hash": workspace.diff_hash(), "script_position": 1, "budgets": loaded.budgets, "task_hash": "unused"})
    (workspace.root / "hello.py").write_text("changed\n", encoding="utf-8")
    command[5] = refusal.run_id
    rejected = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
    assert rejected.returncode == 2
    assert "workspace changed" in rejected.stderr


def test_evalsys_handoff_projects_only_public_fields(tmp_path: Path):
    case = {"problem_statement": "Fix it", "base_commit": "abc", "prompt_variant": "fuzzy", "oracle": "secret", "instance_id": "hidden"}
    request = AgentRunRequest.from_public_case(case, tmp_path)
    projected = request.to_agent_input()
    assert projected == {"task": "Fix it", "repository": str(tmp_path.resolve()), "base_commit": "abc"}
    assert "variant" not in json.dumps(projected).lower()
    with pytest.raises(EvalError, match="explicit confirmation"):
        preflight_agent_config(ROOT / "configs/agent/live-template.json", confirmed=False)
    with pytest.raises(EvalError, match="Frozen baseline"):
        require_frozen_baseline(tmp_path, "baseline-v1")
