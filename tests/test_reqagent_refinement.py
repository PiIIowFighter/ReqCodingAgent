from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reqagent.checkpoint import CheckpointStore
from reqagent.config import AgentConfig
from reqagent.context import ContextLedger
from reqagent.loop import AgentInterrupted, AgentLoop
from reqagent.model import ScriptedModel
from reqagent.tools import build_registry
from reqagent.tools.command import LocalTestCommandExecutor
from reqagent.trace import RunStore
from reqagent.workspace import GitWorkspace


ROOT = Path(__file__).resolve().parents[1]
SLOTS = (
    "goal",
    "current_behavior_or_symptom",
    "expected_behavior",
    "target_component",
    "relevant_symbol_or_api",
    "affected_consumers",
    "compatibility",
    "boundary_and_error_semantics",
    "excluded_scope",
    "acceptance_criteria",
    "relevant_tests_or_checks",
)


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.strip()


def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Test")
    (root / "hello.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(root, "add", "hello.py")
    git(root, "commit", "-qm", "initial")
    return root


def config(tmp_path: Path, script: list[dict]) -> AgentConfig:
    raw = json.loads((ROOT / "configs/agent/offline-scripted.json").read_text(encoding="utf-8"))
    raw["script"] = script
    path = tmp_path / "agent.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return AgentConfig.load(path)


def call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "text": "",
        "tool_calls": [{"call_id": call_id, "name": name, "arguments": arguments}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "finish_reason": "tool_calls",
        "provider_request_id": call_id,
    }


def baseline() -> dict:
    slots = {
        name: {
            "value": "",
            "status": "unresolved",
            "evidence": [],
            "confidence": 0.0,
        }
        for name in SLOTS
    }
    slots.update(
        {
            "goal": {
                "value": "Change the configured value",
                "status": "explicit",
                "evidence": [{"kind": "user_task", "reference": "task", "detail": "Requested change"}],
                "confidence": 1.0,
            },
            "expected_behavior": {
                "value": "The module exposes VALUE = 2",
                "status": "inferred",
                "evidence": [{"kind": "code", "reference": "hello.py:1", "detail": "Existing constant"}],
                "confidence": 0.9,
            },
            "target_component": {
                "value": "hello.py",
                "status": "inferred",
                "evidence": [{"kind": "code", "reference": "hello.py:1", "detail": "Only matching implementation"}],
                "confidence": 0.9,
            },
            "acceptance_criteria": {
                "value": "Importing hello yields VALUE == 2",
                "status": "inferred",
                "evidence": [{"kind": "code", "reference": "hello.py:1", "detail": "Observable module API"}],
                "confidence": 0.85,
            },
        }
    )
    return {
        "ambiguity_types": ["specificity_reduction"],
        "selected_skills": ["specificity_expansion"],
        "slots": slots,
        "assumptions": [
            {
                "value": "Preserve all unrelated behavior",
                "provenance": "Repository contains no evidence requiring a broader change",
                "confidence": 0.8,
            }
        ],
        "original_summary": "Change the value correctly",
        "refined_summary": "Set hello.VALUE to 2 and verify it by import",
    }


def loop_parts(tmp_path: Path, script: list[dict], *, interrupt_after: str | None = None):
    source = repository(tmp_path)
    workspace = GitWorkspace.create(source)
    cfg = config(tmp_path, script)
    store = RunStore.create(tmp_path / "runs")
    ledger = ContextLedger("system", "Change the value correctly", context_window=10000, trigger_ratio=.8, keep_recent_rounds=2)
    registry = build_registry(
        workspace,
        cfg.raw,
        command_executor=LocalTestCommandExecutor(),
        artifact_dir=store.path / "commands",
        requirement_refinement=True,
        task=ledger.task,
    )
    loop = AgentLoop(ScriptedModel(cfg.script), registry, workspace, cfg, ledger, store, interrupt_after=interrupt_after)
    return source, workspace, cfg, store, ledger, registry, loop


def test_mutating_execution_and_submit_are_rejected_before_requirement_baseline(tmp_path: Path):
    source = repository(tmp_path)
    workspace = GitWorkspace.create(source)
    cfg = config(tmp_path, [call("unused", "submit", {"summary": "x", "tests": [], "limitations": ""})])
    registry = build_registry(
        workspace,
        cfg.raw,
        command_executor=LocalTestCommandExecutor(),
        requirement_refinement=True,
        task="Change the value correctly",
    )
    patch = registry.execute(
        "apply_patch",
        {"patch": "--- a/hello.py\n+++ b/hello.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n"},
    )
    command = registry.execute("run_command", {"command": "printf changed", "timeout_seconds": 2})
    submit = registry.execute("submit", {"summary": "done", "tests": [], "limitations": ""})
    assert [result.error["kind"] for result in (patch, command, submit)] == ["requirement_gate"] * 3
    assert workspace.diff() == ""


def test_valid_requirement_baseline_unlocks_existing_agent_loop(tmp_path: Path):
    patch = "--- a/hello.py\n+++ b/hello.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n"
    script = [
        call("baseline", "record_requirement_baseline", baseline()),
        call("patch", "apply_patch", {"patch": patch}),
        call("submit", "submit", {"summary": "Set value", "tests": [], "limitations": ""}),
    ]
    _, workspace, _, store, ledger, registry, loop = loop_parts(tmp_path, script)
    result = loop.run()
    assert result.stop_reason == "submitted"
    assert result.patch.files == 1
    assert registry.refinement.approved is True
    assert sum(message.text.startswith("RequirementBaseline:") for message in ledger.messages) == 1
    evidence = json.loads((store.path / "requirement-baseline.json").read_text(encoding="utf-8"))
    assert evidence["slots"]["target_component"]["status"] == "inferred"
    assert "requirement-baseline.json" in (store.path / "checksums.sha256").read_text(encoding="utf-8")


def test_requirement_state_survives_checkpoint_resume(tmp_path: Path):
    script = [
        call("baseline", "record_requirement_baseline", baseline()),
        call("submit", "submit", {"summary": "No code change", "tests": [], "limitations": ""}),
    ]
    _, workspace, cfg, store, ledger, registry, loop = loop_parts(
        tmp_path,
        script,
        interrupt_after="after_tool_checkpoint",
    )
    with pytest.raises(AgentInterrupted):
        loop.run()
    checkpoint = CheckpointStore(store.path).load()
    assert checkpoint["requirement_refinement"]["baseline"]["selected_skills"] == ["specificity_expansion"]

    resumed_registry = build_registry(
        workspace,
        cfg.raw,
        command_executor=LocalTestCommandExecutor(),
        artifact_dir=store.path / "commands",
        requirement_refinement=True,
        task=ledger.task,
    )
    resumed_ledger = ContextLedger("system", ledger.task, context_window=10000, trigger_ratio=.8, keep_recent_rounds=2)
    resumed_ledger.messages = list(ledger.messages)
    resumed = AgentLoop(ScriptedModel(cfg.script, position=1), resumed_registry, workspace, cfg, resumed_ledger, store)
    resumed.restore(checkpoint)
    result = resumed.run()
    assert result.stop_reason == "submitted"
    assert resumed_registry.refinement.approved is True
    assert resumed_registry.refinement.selected_skills == ("specificity_expansion",)
    trace = json.loads((store.path / "refinement-trace.json").read_text(encoding="utf-8"))
    assert trace["gate"]["passed"] is True
