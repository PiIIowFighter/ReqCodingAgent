from __future__ import annotations

import json
import subprocess
from pathlib import Path

from reqagent.adaptive import AdaptiveRefinementState
from reqagent.checkpoint import CheckpointStore, canonical_hash
from reqagent.config import AgentConfig
from reqagent.context import ContextLedger
from reqagent.loop import AgentLoop, validate_tool_protocol
from reqagent.model import ModelRequest, ModelResponse, ScriptedModel
from reqagent.tools import build_registry
from reqagent.trace import RunStore
from reqagent.workspace import GitWorkspace


ROOT = Path(__file__).resolve().parents[1]
TASK = "Improve the related handling correctly."


def repository(tmp_path: Path) -> GitWorkspace:
    source = tmp_path / "repo"
    source.mkdir(parents=True)
    subprocess.run(["git", "-C", str(source), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
    (source / "code.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "initial"], check=True)
    return GitWorkspace.create(source)


def config(tmp_path: Path, script: list[dict]) -> AgentConfig:
    raw = json.loads((ROOT / "configs/agent/offline-scripted.json").read_text(encoding="utf-8"))
    raw["script"] = script
    path = tmp_path / "agent.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return AgentConfig.load(path)


def response(request_id: str, calls: list[tuple[str, str, dict]]) -> dict:
    return {
        "text": "",
        "tool_calls": [{"call_id": call_id, "name": name, "arguments": arguments} for call_id, name, arguments in calls],
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "finish_reason": "tool_calls" if calls else "stop",
        "provider_request_id": request_id,
    }


def brief(evidence_ids: list[str]) -> dict:
    return {
        "ambiguity_reason": "Behavior is abstract.",
        "chosen_interpretation": "Keep VALUE stable.",
        "targets": ["code.VALUE"],
        "expected_behavior": "VALUE remains stable.",
        "regression_invariants": ["Existing value remains unchanged."],
        "validation_plan": ["Read code.py and verify VALUE."],
        "unresolved_uncertainty": [],
        "evidence_ids": evidence_ids,
        "candidates": [{"interpretation": "Keep VALUE stable.", "task_fit": 4, "repository_support": 4, "compatibility": 4, "testability": 4}],
    }


def run_loop(tmp_path: Path, script: list[dict]):
    workspace = repository(tmp_path)
    cfg = config(tmp_path, script)
    registry = build_registry(workspace, cfg.raw, requirement_refinement="auto", task=TASK)
    system = (ROOT / "prompts/baseline/system.txt").read_text(encoding="utf-8")
    protocol = (ROOT / "prompts/baseline/protocol.txt").read_text(encoding="utf-8")
    ledger = ContextLedger(system + "\n" + protocol, TASK, context_window=10000, trigger_ratio=.8, keep_recent_rounds=2)
    store = RunStore.create(tmp_path / "runs")
    return AgentLoop(ScriptedModel(cfg.script), registry, workspace, cfg, ledger, store), registry, ledger, store


def test_phase_budget_closes_every_call_id_exactly_once(tmp_path: Path):
    calls = [(f"c{index}", "read_file", {"path": "code.py", "start_line": 1, "end_line": index + 1}) for index in range(7)]
    loop, registry, _, store = run_loop(tmp_path, [response("one", calls), response("synthesis", [])])
    loop.run()
    events = [json.loads(line) for line in (store.path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    real_ids = [event["call_id"] for event in events if event.get("kind") == "tool_result"]
    synthetic = [event for event in events if event.get("kind") == "protocol_closure"]
    assert real_ids + [event["call_id"] for event in synthetic] == [call_id for call_id, _, _ in calls]
    assert synthetic[-1]["error_kind"] == "phase_budget_exhausted"
    assert len(set(real_ids + [event["call_id"] for event in synthetic])) == 7
    assert sum(item["result"]["ok"] for item in registry.history if item["name"] == "read_file") == 6


def test_protocol_validator_rejects_orphan_duplicate_and_unknown_results():
    from reqagent.model import ModelMessage, NormalizedToolCall
    assistant = ModelMessage("assistant", tool_calls=(NormalizedToolCall("a", "read_file", {"path": "x"}),))
    for messages in (
        [assistant],
        [assistant, ModelMessage("tool", tool_results=({"call_id": "a"}, {"call_id": "a"}))],
        [assistant, ModelMessage("tool", tool_results=({"call_id": "other"},))],
    ):
        try:
            validate_tool_protocol(messages)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid protocol accepted")


def test_brief_transition_closes_remaining_old_phase_calls_without_execution(tmp_path: Path):
    loop, registry, _, store = run_loop(tmp_path, [
        response("read", [("read", "read_file", {"path": "code.py"})]),
        response("brief", [
            ("brief", "record_requirement_brief", brief(["E001"])),
            ("late", "read_file", {"path": "code.py"}),
        ]),
        response("submit", [("submit", "submit", {"summary": "done", "tests": [], "limitations": ""})]),
    ])
    loop.run()
    events = [json.loads(line) for line in (store.path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    late = next(event for event in events if event.get("kind") == "protocol_closure" and event.get("call_id") == "late")
    assert late["error_kind"] == "phase_transition"
    assert len([item for item in registry.history if item["name"] == "read_file"]) == 1


def test_investigation_threshold_switches_to_synthesis_brief_only(tmp_path: Path):
    captured = []

    class CaptureAdapter:
        identity = {"provider": "scripted"}
        position = 0
        def complete(self, request: ModelRequest) -> ModelResponse:
            captured.append([tool.name for tool in request.tools])
            self.position += 1
            if self.position <= 2:
                return ModelResponse.from_dict(response(str(self.position), [(f"r{self.position}", "read_file", {"path": "code.py"})]))
            return ModelResponse.from_dict(response("synthesis", []))

    loop, registry, _, _ = run_loop(tmp_path, [response("placeholder", [])])
    loop.adapter = CaptureAdapter()
    loop.run()
    assert captured[0] == ["list_files", "read_file", "search_text", "record_requirement_brief"]
    assert captured[1] == captured[0]
    assert captured[2] == ["record_requirement_brief"]
    assert registry.adaptive.refinement_stage == "complete"
    assert registry.adaptive.fallback_reason


def test_first_synthesis_brief_executes_after_five_investigation_tools(tmp_path: Path):
    investigation = [
        response("r1", [
            ("r1a", "read_file", {"path": "code.py"}),
            ("r1b", "search_text", {"query": "VALUE", "path": "."}),
            ("r1c", "list_files", {"path": "."}),
        ]),
        response("r2", [
            ("r2a", "read_file", {"path": "code.py"}),
            ("r2b", "search_text", {"query": "VALUE", "path": "."}),
        ]),
    ]
    loop, registry, _, store = run_loop(tmp_path, investigation + [
        response("synthesis", [
            ("brief", "record_requirement_brief", brief(["E001", "E002", "E003", "E004", "E005"])),
            ("late", "record_requirement_brief", brief(["E001"])),
        ]),
        response("submit", [("submit", "submit", {"summary": "done", "tests": [], "limitations": ""})]),
    ])

    result = loop.run()

    assert result.stop_reason == "submitted"
    brief_calls = [item for item in registry.history if item["name"] == "record_requirement_brief"]
    assert len(brief_calls) == 1
    assert brief_calls[0]["result"]["ok"] is True
    events = [json.loads(line) for line in (store.path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    late = next(event for event in events if event.get("kind") == "protocol_closure" and event.get("call_id") == "late")
    assert late["error_kind"] == "phase_transition"
    assert registry.adaptive.investigation_tool_count == 5
    assert registry.adaptive.synthesis_tool_count == 1
    trace = registry.adaptive.trace()
    checkpoint = registry.adaptive.to_checkpoint()
    assert trace["synthesis_tool_count"] == checkpoint["synthesis_tool_count"] == 1


def test_synthesis_failure_restores_clean_baseline_context(tmp_path: Path):
    loop, registry, ledger, _ = run_loop(tmp_path, [
        response("r1", [("r1", "read_file", {"path": "code.py"})]),
        response("r2", [("r2", "read_file", {"path": "code.py"})]),
        response("synthesis", []),
        response("submit", [("submit", "submit", {"summary": "done", "tests": [], "limitations": ""})]),
    ])
    result = loop.run()
    assert result.stop_reason == "submitted"
    assert registry.adaptive.fallback_reason
    assert [message.role for message in ledger.messages[:2]] == ["system", "user"]
    assert ledger.messages[1].text == TASK
    assert len([message for message in ledger.messages if message.role == "system"]) == 1
    assert [tool.name for tool in registry.definitions] == ["list_files", "read_file", "search_text", "apply_patch", "run_command", "submit"]


def test_fail_open_checkpoint_json_resume_is_coding_and_closed(tmp_path: Path):
    state = AdaptiveRefinementState(TASK)
    state.transition_to_synthesis()
    state.fail_open_refinement("invalid synthesis")
    payload = {"adaptive": state.to_checkpoint(), "pending_phase": "synthesis", "closed_call_ids": ["a"]}
    run = tmp_path / "checkpoint"
    run.mkdir()
    store = CheckpointStore(run)
    store.save(1, payload)
    loaded = store.load()
    restored = AdaptiveRefinementState(TASK)
    restored.restore(loaded["adaptive"])
    assert restored.phase == "coding"
    assert restored.refinement_stage == "complete"
    assert restored.fallback_reason == "invalid synthesis"
    assert loaded["closed_call_ids"] == ["a"]
