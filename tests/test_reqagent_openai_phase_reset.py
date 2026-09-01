from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from reqagent.config import AgentConfig
from reqagent.context import ContextLedger
from reqagent.loop import AgentLoop
from reqagent.openai_responses import OpenAIResponsesAdapter
from reqagent.tools import build_registry
from reqagent.trace import RunStore
from reqagent.workspace import GitWorkspace


ROOT = Path(__file__).resolve().parents[1]
TASK = "Improve the related handling correctly."
BASE_TOOLS = ["list_files", "read_file", "search_text", "apply_patch", "run_command", "submit"]


def _config(tmp_path: Path) -> AgentConfig:
    raw = json.loads((ROOT / "configs/agent/demo-openai.json").read_text(encoding="utf-8"))
    path = tmp_path / "agent.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return AgentConfig.load(path)


def _workspace(tmp_path: Path) -> GitWorkspace:
    source = tmp_path / "repo"
    source.mkdir()
    subprocess.run(["git", "-C", str(source), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
    (source / "code.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "initial"], check=True)
    return GitWorkspace.create(source)


def _response(request_id: str, call_id: str, name: str, arguments: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=request_id,
        model="gpt-4o-mini",
        status="completed",
        usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2),
        output=[{
            "type": "function_call",
            "call_id": call_id,
            "name": name,
            "arguments": json.dumps(arguments),
        }],
    )


def _brief() -> dict:
    return {
        "ambiguity_reason": "Behavior is abstract.",
        "chosen_interpretation": "Keep VALUE stable.",
        "targets": ["code.VALUE"],
        "expected_behavior": "VALUE remains stable.",
        "regression_invariants": ["Existing value remains unchanged."],
        "validation_plan": ["Read code.py and verify VALUE."],
        "unresolved_uncertainty": [],
        "evidence_ids": ["E001", "E002"],
        "candidates": [{
            "interpretation": "Keep VALUE stable.",
            "task_fit": 4,
            "repository_support": 4,
            "compatibility": 4,
            "testability": 4,
        }],
    }


class StrictPhaseResponses:
    def __init__(self, *, fail_synthesis: bool = False):
        self.calls: list[dict] = []
        self.fail_synthesis = fail_synthesis

    @staticmethod
    def _assert_closed(input_items: list[dict]) -> None:
        calls = [item["call_id"] for item in input_items if item.get("type") == "function_call"]
        outputs = [item["call_id"] for item in input_items if item.get("type") == "function_call_output"]
        assert len(outputs) == len(set(outputs))
        assert calls == outputs, f"orphan Responses calls: calls={calls}, outputs={outputs}"

    def create(self, **kwargs):
        self.calls.append(kwargs)
        request_number = len(self.calls)
        input_items = kwargs["input"]
        self._assert_closed(input_items)
        tool_names = [tool["name"] for tool in kwargs["tools"]]

        if request_number == 1:
            assert tool_names == ["list_files", "read_file", "search_text"]
            return _response("investigation-1", "list-1", "list_files", {"path": "."})
        if request_number == 2:
            assert tool_names == ["list_files", "read_file", "search_text"]
            return _response("investigation-2", "search-2", "search_text", {"query": "VALUE", "path": "."})
        if request_number == 3:
            assert tool_names == ["record_requirement_brief"]
            assert input_items == [{"role": "user", "content": TASK}]
            assert "E001:" in kwargs["instructions"] and "E002:" in kwargs["instructions"]
            if self.fail_synthesis:
                error = RuntimeError("provider body must not be persisted")
                error.status_code = 400
                error.request_id = "req-safe-400"
                raise error
            return _response("synthesis", "brief-3", "record_requirement_brief", _brief())
        if request_number == 4:
            assert tool_names == BASE_TOOLS
            assert input_items == [{"role": "user", "content": TASK}]
            if self.fail_synthesis:
                assert "RequirementBrief" not in kwargs["instructions"]
                return _response("main-fail-open", "submit-4", "submit", {
                    "summary": "Continued safely from the original task.",
                    "tests": [],
                    "limitations": "Refinement failed open.",
                })
            assert "RequirementBrief" in kwargs["instructions"]
            return _response("main-1", "read-4", "read_file", {"path": "code.py"})
        if request_number == 5:
            assert tool_names == BASE_TOOLS
            return _response("main-2", "submit-5", "submit", {
                "summary": "Verified the focused behavior.",
                "tests": [],
                "limitations": "No source change was required.",
            })
        raise AssertionError("unexpected Responses request")


class FakeOpenAIClient:
    def __init__(self, responses: StrictPhaseResponses):
        self.responses = responses


class RejectingResponses:
    def create(self, **kwargs):
        error = RuntimeError("raw provider body with C:\\secret\\path and api_key=hidden")
        error.status_code = 400
        error.request_id = "req-final-400"
        raise error


def test_responses_wire_history_resets_across_investigation_synthesis_and_main(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
    config = _config(tmp_path)
    workspace = _workspace(tmp_path)
    fake = StrictPhaseResponses()
    adapter = OpenAIResponsesAdapter(config, client=FakeOpenAIClient(fake))
    registry = build_registry(workspace, config.raw, requirement_refinement="auto", task=TASK)
    system = (ROOT / "prompts/baseline/system.txt").read_text(encoding="utf-8")
    protocol = (ROOT / "prompts/baseline/protocol.txt").read_text(encoding="utf-8")
    context = ContextLedger(system + "\n" + protocol, TASK, context_window=10000, trigger_ratio=.8, keep_recent_rounds=2)
    store = RunStore.create(tmp_path / "runs")

    result = AgentLoop(adapter, registry, workspace, config, context, store).run()

    assert result.stop_reason == "submitted"
    assert len(fake.calls) == 5
    assert registry.adaptive.brief is not None
    assert adapter.actual_model == "gpt-4o-mini"


def test_responses_synthesis_failure_resets_before_main_and_records_safe_error(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
    config = _config(tmp_path)
    workspace = _workspace(tmp_path)
    fake = StrictPhaseResponses(fail_synthesis=True)
    adapter = OpenAIResponsesAdapter(config, client=FakeOpenAIClient(fake))
    registry = build_registry(workspace, config.raw, requirement_refinement="auto", task=TASK)
    system = (ROOT / "prompts/baseline/system.txt").read_text(encoding="utf-8")
    protocol = (ROOT / "prompts/baseline/protocol.txt").read_text(encoding="utf-8")
    context = ContextLedger(system + "\n" + protocol, TASK, context_window=10000, trigger_ratio=.8, keep_recent_rounds=2)
    store = RunStore.create(tmp_path / "runs")

    result = AgentLoop(adapter, registry, workspace, config, context, store).run()

    assert result.stop_reason == "submitted"
    assert registry.adaptive.fallback_reason
    events = [json.loads(line) for line in (store.path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    error = next(event for event in events if event["kind"] == "model_error")
    assert error == {
        "kind": "model_error",
        "phase": "refinement",
        "category": "request",
        "retryable": False,
        "attempt_count": 1,
        "provider_request_id": "req-safe-400",
        "status_code": 400,
        "configured_model": "gpt-4o-mini",
        "actual_model": "gpt-4o-mini",
    }
    serialized = json.dumps(events)
    assert "provider body" not in serialized
    assert "test-placeholder" not in serialized


def test_final_model_failure_records_structured_safe_error_before_stopping(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
    config = _config(tmp_path)
    workspace = _workspace(tmp_path)
    adapter = OpenAIResponsesAdapter(config, client=FakeOpenAIClient(RejectingResponses()))
    registry = build_registry(workspace, config.raw, task=TASK)
    context = ContextLedger("system", TASK, context_window=10000, trigger_ratio=.8, keep_recent_rounds=2)
    store = RunStore.create(tmp_path / "runs")

    result = AgentLoop(adapter, registry, workspace, config, context, store).run()

    assert result.stop_reason == "unrecoverable_model_error"
    events = [json.loads(line) for line in (store.path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    error = next(event for event in events if event["kind"] == "model_error")
    assert error == {
        "kind": "model_error",
        "phase": "main",
        "category": "request",
        "retryable": False,
        "attempt_count": 1,
        "provider_request_id": "req-final-400",
        "status_code": 400,
        "configured_model": "gpt-4o-mini",
    }
    serialized = json.dumps(events)
    assert "secret" not in serialized
    assert "hidden" not in serialized
