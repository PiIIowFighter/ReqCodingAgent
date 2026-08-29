from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from reqagent.cli import run_kind
from reqagent.config import AgentConfig
from reqagent.live import AnthropicMessagesAdapter, build_live_runtime
from reqagent.model import ModelError, ModelMessage, ModelRequest, ToolDefinition
from reqagent.tools.command import ContainerCommandExecutor


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_live_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9/")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-placeholder")


class FakeMessages:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


class FakeClient:
    def __init__(self, messages):
        self.messages = messages


def config(tmp_path: Path) -> AgentConfig:
    raw = json.loads((ROOT / "configs/agent/live-local-proxy.json").read_text(encoding="utf-8"))
    path = tmp_path / "live.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return AgentConfig.load(path)


def request() -> ModelRequest:
    schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}
    return ModelRequest(
        messages=(
            ModelMessage("system", "system prompt"),
            ModelMessage("user", "inspect"),
            ModelMessage("assistant", "working", tool_calls=()),
            ModelMessage("tool", tool_results=({"call_id": "old", "ok": True, "tool": "read_file", "data": {"content": "x"}, "error": None, "truncated": False, "meta": {}},)),
        ),
        tools=(ToolDefinition("read_file", "read", schema),),
        max_output_tokens=4096,
        timeout_seconds=180,
    )


def test_adapter_converts_messages_tools_usage_and_multiple_calls(tmp_path: Path):
    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="checking"), SimpleNamespace(type="tool_use", id="one", name="read_file", input={"path": "a.py"}), SimpleNamespace(type="tool_use", id="two", name="read_file", input={"path": "b.py"})],
        usage=SimpleNamespace(input_tokens=12, output_tokens=7), stop_reason="tool_use",
        model="gpt-5.6-sol", _request_id="request-1",
    )
    messages = FakeMessages(response=response)
    adapter = AnthropicMessagesAdapter(config(tmp_path), client=FakeClient(messages))
    normalized = adapter.complete(request())
    sent = messages.calls[0]
    assert sent["system"] == "system prompt"
    assert sent["model"] == "gpt-5.6-sol"
    assert "temperature" not in sent and "seed" not in sent
    assert "strict" not in sent["tools"][0]
    assert sent["tools"][0]["input_schema"]["additionalProperties"] is False
    assert sent["messages"][-1]["content"][0]["type"] == "tool_result"
    assert [call.call_id for call in normalized.tool_calls] == ["one", "two"]
    assert normalized.usage == {"input_tokens": 12, "output_tokens": 7}
    assert normalized.finish_reason == "tool_calls"
    assert normalized.provider_request_id == "request-1"
    assert normalized.actual_model == "gpt-5.6-sol"


def test_adapter_rejects_unknown_tool_and_bad_arguments_locally(tmp_path: Path):
    for block in (
        SimpleNamespace(type="tool_use", id="one", name="unknown", input={}),
        SimpleNamespace(type="tool_use", id="one", name="read_file", input={"path": "a", "extra": True}),
        SimpleNamespace(type="tool_use", id="one", name="read_file", input={"path": 3}),
    ):
        response = SimpleNamespace(content=[block], usage=SimpleNamespace(input_tokens=1, output_tokens=1), stop_reason="tool_use", model="gpt-5.6-sol", _request_id=None)
        adapter = AnthropicMessagesAdapter(config(tmp_path), client=FakeClient(FakeMessages(response=response)))
        with pytest.raises(ModelError, match="malformed") as caught:
            adapter.complete(request())
        assert caught.value.retryable is False


@pytest.mark.parametrize(("status", "category", "retryable"), [(429, "rate_limit", True), (503, "server", True), (401, "authentication", False), (400, "request", False)])
def test_adapter_classifies_status_errors_without_secret_text(tmp_path: Path, status: int, category: str, retryable: bool):
    class StatusError(Exception):
        pass
    error = StatusError("credential=secret-value")
    error.status_code = status
    adapter = AnthropicMessagesAdapter(config(tmp_path), client=FakeClient(FakeMessages(error=error)))
    with pytest.raises(ModelError) as caught:
        adapter.complete(request())
    assert caught.value.category == category and caught.value.retryable is retryable
    assert "secret-value" not in str(caught.value)


@pytest.mark.parametrize(("error", "category"), [(TimeoutError("token=abc"), "timeout"), (ConnectionError("token=abc"), "connection")])
def test_adapter_classifies_transport_errors(tmp_path: Path, error: Exception, category: str):
    adapter = AnthropicMessagesAdapter(config(tmp_path), client=FakeClient(FakeMessages(error=error)))
    with pytest.raises(ModelError) as caught:
        adapter.complete(request())
    assert caught.value.category == category and caught.value.retryable
    assert "abc" not in str(caught.value)


def test_live_validation_without_proxy_environment_is_stable_and_offline(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    cfg = config(tmp_path)
    with pytest.raises(ValueError, match="live configuration is incomplete") as caught:
        cfg.validate(live=True)
    assert "ANTHROPIC_BASE_URL" in str(caught.value)
    assert "ANTHROPIC_AUTH_TOKEN" in str(caught.value)


def test_live_runtime_uses_env_references_and_container_executor(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:4141/?secret=hidden")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "never-store-this")
    cfg = config(tmp_path)
    adapter, executor = build_live_runtime(cfg, run_id="run-one", client=FakeClient(FakeMessages()), inspect_image=False)
    assert isinstance(adapter, AnthropicMessagesAdapter)
    assert isinstance(executor, ContainerCommandExecutor)
    assert adapter.identity["provider"] == "local_reverse_proxy"
    serialized = json.dumps(cfg.public_dict())
    assert "never-store-this" not in serialized and "secret=hidden" not in serialized
    assert cfg.public_dict()["model"]["api_key_env"]["env"] == "ANTHROPIC_AUTH_TOKEN"


def test_live_resume_identity_changes_when_endpoint_or_model_changes(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:4141/")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-token")
    cfg = config(tmp_path)
    adapter, _ = build_live_runtime(cfg, run_id="run-one", client=FakeClient(FakeMessages()), inspect_image=False)
    first = adapter.identity
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:4242/")
    changed, _ = build_live_runtime(cfg, run_id="run-one", client=FakeClient(FakeMessages()), inspect_image=False)
    assert first != changed.identity


def test_run_kind_distinguishes_live_and_scripted():
    assert run_kind("live") == "live"
    assert run_kind("scripted") == "offline"


def test_container_argv_uses_pull_never_and_converted_mount(tmp_path: Path):
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "ok", "")
    executor = ContainerCommandExecutor(command_prefix=("wsl.exe", "--", "docker"), image="ubuntu@sha256:" + "4" * 64, run_id="run", runner=runner, path_converter=lambda _: "/mnt/task")
    workspace = SimpleNamespace(root=tmp_path)
    executor.execute(workspace, tmp_path, "grep VALUE value.txt", 5, tmp_path / "out", tmp_path / "err")
    argv = calls[0]
    assert argv[:4] == ["wsl.exe", "--", "docker", "run"]
    assert argv[argv.index("--pull") + 1] == "never"
    assert "type=bind,src=/mnt/task,dst=/workspace" in argv
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert not any("TOKEN" in part or "API_KEY" in part or "localhost:4141" in part for part in argv)
