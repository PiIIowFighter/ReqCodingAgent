from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from reqagent.checkpoint import CheckpointStore
from reqagent.config import AgentConfig
from reqagent.context import ContextLedger
from reqagent.loop import AgentLoop
from reqagent.model import ModelResponse, ScriptedModel
from reqagent.patching import apply_patch_atomic
from reqagent.tools import build_registry
from reqagent.trace import RunStore
from reqagent.workspace import GitWorkspace, WorkspacePolicy, WorkspaceViolation


ROOT = Path(__file__).resolve().parents[1]


def git(root: Path, *args: str) -> str:
    completed = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8", check=True)
    return completed.stdout.strip()


def repository(tmp_path: Path) -> Path:
    root = tmp_path / "合成 repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Test")
    (root / "hello.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(root, "add", "hello.py")
    git(root, "commit", "-qm", "initial")
    return root


def config(tmp_path: Path, script: list[dict], **budget_overrides) -> AgentConfig:
    source = json.loads((ROOT / "configs/agent/offline-scripted.json").read_text(encoding="utf-8"))
    source["script"] = script
    source["budgets"].update(budget_overrides)
    path = tmp_path / "agent.json"
    path.write_text(json.dumps(source), encoding="utf-8")
    return AgentConfig.load(path)


def response(call_id: str, name: str, arguments: dict) -> dict:
    return {"text": "", "tool_calls": [{"call_id": call_id, "name": name, "arguments": arguments}], "usage": {"input_tokens": 1, "output_tokens": 1}, "finish_reason": "tool_calls", "provider_request_id": call_id}


def test_config_and_live_doctor_refusal(tmp_path: Path):
    loaded = AgentConfig.load(ROOT / "configs/agent/offline-scripted.json")
    assert loaded.mode == "scripted"
    with pytest.raises(ValueError, match="live configuration is incomplete"):
        AgentConfig.load(ROOT / "configs/agent/live-template.json").validate(live=True)
    malformed = json.loads((ROOT / "configs/agent/offline-scripted.json").read_text(encoding="utf-8"))
    malformed["unknown"] = True
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(malformed), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid config keys"):
        AgentConfig.load(path)


def test_workspace_rejects_absolute_traversal_git_and_symlink(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    policy = WorkspacePolicy(root)
    for value in (str(outside.resolve()), "../outside", ".git/config"):
        with pytest.raises(WorkspaceViolation):
            policy.resolve(value)
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(WorkspaceViolation):
        policy.resolve("link/file.txt")


def test_apply_patch_is_atomic_on_failure(tmp_path: Path):
    source = repository(tmp_path)
    workspace = GitWorkspace.create(source)
    before = (workspace.root / "hello.py").read_text(encoding="utf-8")
    bad = "--- a/hello.py\n+++ b/hello.py\n@@ -1 +1 @@\n-NOT PRESENT\n+VALUE = 2\n"
    with pytest.raises(ValueError, match="patch check failed"):
        apply_patch_atomic(workspace, bad)
    assert (workspace.root / "hello.py").read_text(encoding="utf-8") == before


def test_run_command_success_nonzero_and_timeout(tmp_path: Path):
    workspace = GitWorkspace.create(repository(tmp_path))
    cfg = AgentConfig.load(ROOT / "configs/agent/offline-scripted.json")
    registry = build_registry(workspace, cfg.raw)
    ok = registry.execute("run_command", {"command": "printf ok", "timeout_seconds": 2})
    assert ok.ok and ok.data["stdout"] == "ok"
    failed = registry.execute("run_command", {"command": "printf bad >&2; exit 7", "timeout_seconds": 2})
    assert not failed.ok and failed.data["exit_code"] == 7
    timeout = registry.execute("run_command", {"command": "sleep 2", "timeout_seconds": 1})
    assert not timeout.ok and timeout.data["timed_out"]
    network = registry.execute("run_command", {"command": "curl https://example.com", "timeout_seconds": 1})
    assert not network.ok and network.error["kind"] == "tool_error"


def test_loop_rejects_invalid_call_without_execution(tmp_path: Path):
    source = repository(tmp_path)
    workspace = GitWorkspace.create(source)
    cfg = config(tmp_path, [response("bad", "apply_patch", {"unknown": "x"})], max_invalid_outputs=1)
    store = RunStore.create(tmp_path / "runs")
    ledger = ContextLedger("system", "task", context_window=10000, trigger_ratio=.8, keep_recent_rounds=2)
    result = AgentLoop(ScriptedModel(cfg.script), build_registry(workspace, cfg.raw), workspace, cfg, ledger, store).run()
    assert result.stop_reason == "invalid_output_limit"
    assert workspace.diff() == ""


def test_fake_model_end_to_end_and_patch_applies_fresh(tmp_path: Path):
    source = repository(tmp_path)
    patch = "--- a/hello.py\n+++ b/hello.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n"
    script = [
        response("1", "search_text", {"query": "VALUE", "path": ".", "glob": "*.py"}),
        response("2", "read_file", {"path": "hello.py"}),
        response("3", "apply_patch", {"patch": patch}),
        response("4", "run_command", {"command": "python -c \"import hello; assert hello.VALUE == 2\"", "timeout_seconds": 10}),
        response("5", "submit", {"summary": "Update value", "tests": ["import hello"], "limitations": ""}),
    ]
    cfg = config(tmp_path, script)
    workspace = GitWorkspace.create(source)
    store = RunStore.create(tmp_path / "runs")
    ledger = ContextLedger("system", "change value", context_window=10000, trigger_ratio=.8, keep_recent_rounds=2)
    result = AgentLoop(ScriptedModel(cfg.script), build_registry(workspace, cfg.raw), workspace, cfg, ledger, store).run()
    assert result.stop_reason == "submitted"
    assert result.patch.files == 1 and result.patch.additions == 1
    fresh = tmp_path / "fresh"
    subprocess.run(["git", "clone", "-q", str(source), str(fresh)], check=True)
    patch_file = tmp_path / "result.patch"
    patch_file.write_bytes(result.patch.text.encode("utf-8"))
    subprocess.run(["git", "-C", str(fresh), "apply", str(patch_file)], check=True)
    assert (fresh / "hello.py").read_text(encoding="utf-8") == "VALUE = 2\n"


def test_repeated_action_stops(tmp_path: Path):
    source = repository(tmp_path)
    repeated = response("1", "read_file", {"path": "hello.py"})
    cfg = config(tmp_path, [repeated, {**repeated, "provider_request_id": "2"}, {**repeated, "provider_request_id": "3"}])
    workspace = GitWorkspace.create(source)
    store = RunStore.create(tmp_path / "runs")
    ledger = ContextLedger("system", "task", context_window=10000, trigger_ratio=.8, keep_recent_rounds=2)
    result = AgentLoop(ScriptedModel(cfg.script), build_registry(workspace, cfg.raw), workspace, cfg, ledger, store).run()
    assert result.stop_reason == "repeated_action"


def test_checkpoint_save_load_and_refusals(tmp_path: Path):
    run = tmp_path / "run"
    run.mkdir()
    store = CheckpointStore(run)
    payload = {"workspace": "abc", "budgets": {"steps": 1}}
    store.save(1, payload)
    assert store.load() == payload
    latest = run / "checkpoints" / "000001.json"
    value = json.loads(latest.read_text(encoding="utf-8"))
    value["payload"]["workspace"] = "changed"
    latest.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        store.load()
    store.save(2, payload)
    (run / "COMPLETE").write_text("complete\n", encoding="utf-8")
    with pytest.raises(ValueError, match="completed"):
        store.load()


def test_cli_help_doctor_and_dependency_direction():
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    for args in (["--help"], ["doctor", "--help"], ["run", "--help"], ["resume", "--help"]):
        completed = subprocess.run([sys.executable, "-m", "reqagent.cli", *args], cwd=ROOT, env=env, capture_output=True, text=True)
        assert completed.returncode == 0
    doctor = subprocess.run([sys.executable, "-m", "reqagent.cli", "doctor", "--config", str(ROOT / "configs/agent/offline-scripted.json")], cwd=ROOT, env=env, capture_output=True, text=True)
    assert doctor.returncode == 0
    live = subprocess.run([sys.executable, "-m", "reqagent.cli", "doctor", "--config", str(ROOT / "configs/agent/live-template.json"), "--live"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert live.returncode == 2 and "incomplete" in live.stderr
    imports = "import sys, reqagent; assert not any(n == 'evalsys' or n.startswith('evalsys.') for n in sys.modules)"
    clean = subprocess.run([sys.executable, "-c", imports], cwd=ROOT, env=env)
    assert clean.returncode == 0
