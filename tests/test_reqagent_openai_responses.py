from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from reqagent.config import AgentConfig
from reqagent.live import AnthropicMessagesAdapter, build_live_runtime
from reqagent.model import ModelError, ModelMessage, ModelRequest, ToolDefinition
from reqagent.openai_responses import OpenAIResponsesAdapter


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_openai_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


class FakeResponses:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


class FakeOpenAIClient:
    def __init__(self, responses: FakeResponses):
        self.responses = responses


def openai_config(tmp_path: Path) -> AgentConfig:
    raw = json.loads((ROOT / "configs/agent/demo-openai.json").read_text(encoding="utf-8"))
    path = tmp_path / "openai.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return AgentConfig.load(path)


def anthropic_config(tmp_path: Path) -> AgentConfig:
    raw = json.loads((ROOT / "configs/agent/live-local-proxy.json").read_text(encoding="utf-8"))
    path = tmp_path / "anthropic.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return AgentConfig.load(path)


def read_schema() -> dict:
    return {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    }


def tool_result(call_id: str, *, ok: bool = True, tool: str = "read_file", content: str = "x") -> dict:
    return {
        "call_id": call_id,
        "ok": ok,
        "tool": tool,
        "data": {"content": content},
        "error": None,
        "truncated": False,
        "meta": {},
    }


def initial_request() -> ModelRequest:
    return ModelRequest(
        messages=(
            ModelMessage("system", "system prompt"),
            ModelMessage("user", "inspect"),
        ),
        tools=(ToolDefinition("read_file", "read", read_schema()),),
        max_output_tokens=4096,
        timeout_seconds=300,
    )


def followup_request() -> ModelRequest:
    return ModelRequest(
        messages=(
            ModelMessage("system", "system prompt"),
            ModelMessage("user", "inspect"),
            ModelMessage("assistant", "working", tool_calls=( )),
            ModelMessage(
                "tool",
                tool_results=(tool_result("call-1"),),
            ),
        ),
        tools=(ToolDefinition("read_file", "read", read_schema()),),
        max_output_tokens=4096,
        timeout_seconds=300,
    )


def test_openai_adapter_maps_instructions_user_and_function_tools(tmp_path: Path):
    response = SimpleNamespace(
        id="resp-1",
        model="gpt-4o-mini",
        status="completed",
        usage=SimpleNamespace(input_tokens=11, output_tokens=4, total_tokens=15),
        output=[{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}],
    )
    fake = FakeResponses(response=response)
    adapter = OpenAIResponsesAdapter(openai_config(tmp_path), client=FakeOpenAIClient(fake))
    normalized = adapter.complete(initial_request())
    sent = fake.calls[0]
    assert sent["instructions"] == "system prompt"
    assert sent["model"] == "gpt-4o-mini"
    assert sent["store"] is False
    assert sent["input"] == [{"role": "user", "content": "inspect"}]
    assert sent["tools"] == [{
        "type": "function",
        "name": "read_file",
        "description": "read",
        "parameters": read_schema(),
    }]
    assert normalized.finish_reason == "stop"
    assert normalized.provider_request_id == "resp-1"
    assert normalized.actual_model == "gpt-4o-mini"
    assert normalized.usage == {"input_tokens": 11, "output_tokens": 4, "total_tokens": 15}


def test_openai_adapter_parses_single_and_multiple_function_calls(tmp_path: Path):
    response = SimpleNamespace(
        id="resp-2",
        model="gpt-4o-mini",
        status="completed",
        usage=SimpleNamespace(input_tokens=9, output_tokens=8),
        output=[
            {"type": "function_call", "call_id": "one", "name": "read_file", "arguments": '{"path":"a.py"}'},
            {"type": "function_call", "call_id": "two", "name": "read_file", "arguments": '{"path":"b.py"}'},
        ],
    )
    adapter = OpenAIResponsesAdapter(openai_config(tmp_path), client=FakeOpenAIClient(FakeResponses(response=response)))
    normalized = adapter.complete(initial_request())
    assert [call.call_id for call in normalized.tool_calls] == ["one", "two"]
    assert normalized.finish_reason == "tool_calls"


def test_openai_adapter_rejects_unknown_tool_and_bad_arguments_locally(tmp_path: Path):
    for arguments in ('{"path":"a.py","extra":true}', '{"path":3}', '"not-an-object"'):
        response = SimpleNamespace(
            id="resp-bad",
            model="gpt-4o-mini",
            status="completed",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            output=[{"type": "function_call", "call_id": "one", "name": "read_file", "arguments": arguments}],
        )
        adapter = OpenAIResponsesAdapter(openai_config(tmp_path), client=FakeOpenAIClient(FakeResponses(response=response)))
        with pytest.raises(ModelError, match="malformed"):
            adapter.complete(initial_request())


def test_openai_adapter_second_round_preserves_reasoning_and_function_call_output(tmp_path: Path):
    first = SimpleNamespace(
        id="resp-a",
        model="gpt-4o-mini",
        status="completed",
        usage=SimpleNamespace(input_tokens=3, output_tokens=5),
        output=[
            {"type": "reasoning", "id": "rs-1", "encrypted_content": "secret-chain", "summary": []},
            {"type": "function_call", "call_id": "call-1", "name": "read_file", "arguments": '{"path":"a.py"}'},
        ],
    )
    second = SimpleNamespace(
        id="resp-b",
        model="gpt-4o-mini",
        status="completed",
        usage=SimpleNamespace(input_tokens=8, output_tokens=2),
        output=[{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}],
    )
    fake = FakeResponses(response=first)
    adapter = OpenAIResponsesAdapter(openai_config(tmp_path), client=FakeOpenAIClient(fake))
    first_result = adapter.complete(initial_request())
    assert first_result.tool_calls[0].call_id == "call-1"
    fake.response = second
    adapter.complete(followup_request())
    sent = fake.calls[1]["input"]
    assert sent[0] == {"role": "user", "content": "inspect"}
    assert sent[1]["type"] == "reasoning"
    assert sent[1]["encrypted_content"] == "secret-chain"
    assert sent[2]["type"] == "function_call"
    assert sent[3] == {
        "type": "function_call_output",
        "call_id": "call-1",
        "output": json.dumps({"ok": True, "tool": "read_file", "data": {"content": "x"}, "error": None, "truncated": False, "meta": {}}, ensure_ascii=False, sort_keys=True),
    }


def test_openai_adapter_reasoning_does_not_leak_into_model_response_or_errors(tmp_path: Path):
    response = SimpleNamespace(
        id="resp-r",
        model="gpt-4o-mini",
        status="completed",
        usage=SimpleNamespace(input_tokens=2, output_tokens=3),
        output=[
            {"type": "reasoning", "id": "rs-1", "encrypted_content": "secret-chain", "summary": []},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "visible"}]},
        ],
    )
    adapter = OpenAIResponsesAdapter(openai_config(tmp_path), client=FakeOpenAIClient(FakeResponses(response=response)))
    normalized = adapter.complete(initial_request())
    serialized = json.dumps(normalized.to_dict())
    assert normalized.text == "visible"
    assert "secret-chain" not in serialized
    assert "encrypted_content" not in serialized


@pytest.mark.parametrize(("status", "category", "retryable"), [
    (429, "rate_limit", True),
    (503, "server", True),
    (401, "authentication", False),
    (404, "request", False),
    (400, "request", False),
])
def test_openai_adapter_classifies_status_errors_without_secret_text(tmp_path: Path, status: int, category: str, retryable: bool):
    class StatusError(Exception):
        pass

    error = StatusError("credential=secret-value")
    error.status_code = status
    adapter = OpenAIResponsesAdapter(openai_config(tmp_path), client=FakeOpenAIClient(FakeResponses(error=error)))
    with pytest.raises(ModelError) as caught:
        adapter.complete(initial_request())
    assert caught.value.category == category
    assert caught.value.retryable is retryable
    assert "secret-value" not in str(caught.value)


@pytest.mark.parametrize(("error", "category"), [
    (TimeoutError("token=abc"), "timeout"),
    (ConnectionError("token=abc"), "connection"),
])
def test_openai_adapter_classifies_transport_errors(tmp_path: Path, error: Exception, category: str):
    adapter = OpenAIResponsesAdapter(openai_config(tmp_path), client=FakeOpenAIClient(FakeResponses(error=error)))
    with pytest.raises(ModelError) as caught:
        adapter.complete(initial_request())
    assert caught.value.category == category
    assert caught.value.retryable
    assert "abc" not in str(caught.value)


def test_build_live_runtime_selects_anthropic_or_openai_adapter(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:4141/")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "anthropic-token")
    anthropic, _ = build_live_runtime(anthropic_config(tmp_path), run_id="run-one", client=FakeOpenAIClient(FakeResponses()), inspect_image=False)
    assert isinstance(anthropic, AnthropicMessagesAdapter)
    openai, _ = build_live_runtime(openai_config(tmp_path), run_id="run-one", client=FakeOpenAIClient(FakeResponses()), inspect_image=False)
    assert isinstance(openai, OpenAIResponsesAdapter)


def test_demo_config_and_launch_script_target_openai_responses():
    config = json.loads((ROOT / "configs/agent/demo-openai.json").read_text(encoding="utf-8"))
    script = (ROOT / "demo_gui/start_openai.ps1").read_text(encoding="utf-8")
    assert config["model"]["protocol"] == "openai_responses"
    assert config["model"]["model"] == "gpt-4o-mini"
    assert config["model"]["base_url_env"] == "OPENAI_BASE_URL"
    assert config["model"]["api_key_env"] == "OPENAI_API_KEY"
    assert "OPENAI_BASE_URL" not in script or "https://" not in script
    assert "OPENAI_API_KEY" in script
    assert "ANTHROPIC_BASE_URL" not in script
    assert "ANTHROPIC_AUTH_TOKEN" not in script


def test_openai_live_validation_requires_openai_environment(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = openai_config(tmp_path)
    with pytest.raises(ValueError, match="live configuration is incomplete") as caught:
        cfg.validate(live=True)
    assert "OPENAI_BASE_URL" in str(caught.value)
    assert "OPENAI_API_KEY" in str(caught.value)


def test_openai_public_dict_redacts_secrets(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1?secret=hidden")
    monkeypatch.setenv("OPENAI_API_KEY", "never-store-this")
    cfg = openai_config(tmp_path)
    serialized = json.dumps(cfg.public_dict())
    assert "never-store-this" not in serialized
    assert "secret=hidden" not in serialized


def test_openai_adapter_refreshes_instructions_when_system_message_changes(tmp_path: Path):
    response = SimpleNamespace(
        id="resp-sys",
        model="gpt-4o-mini",
        status="completed",
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        output=[{"type": "function_call", "call_id": "call-1", "name": "read_file", "arguments": '{"path":"a.py"}'}],
    )
    fake = FakeResponses(response=response)
    adapter = OpenAIResponsesAdapter(openai_config(tmp_path), client=FakeOpenAIClient(fake))
    adapter.complete(initial_request())
    fake.response = SimpleNamespace(
        id="resp-sys-2",
        model="gpt-4o-mini",
        status="completed",
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        output=[{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}],
    )
    updated = ModelRequest(
        messages=(
            ModelMessage("system", "updated system"),
            ModelMessage("user", "inspect"),
            ModelMessage("assistant", "working", tool_calls=()),
            ModelMessage("tool", tool_results=(tool_result("call-1"),)),
        ),
        tools=(ToolDefinition("read_file", "read", read_schema()),),
        max_output_tokens=4096,
        timeout_seconds=300,
    )
    adapter.complete(updated)
    assert fake.calls[0]["instructions"] == "system prompt"
    assert fake.calls[1]["instructions"] == "updated system"


def test_openai_adapter_deduplicates_tool_results_by_call_id_after_context_shrink(tmp_path: Path):
    first = SimpleNamespace(
        id="resp-a",
        model="gpt-4o-mini",
        status="completed",
        usage=SimpleNamespace(input_tokens=3, output_tokens=5),
        output=[
            {"type": "reasoning", "id": "rs-1", "encrypted_content": "secret-chain", "summary": []},
            {"type": "function_call", "call_id": "call-1", "name": "read_file", "arguments": '{"path":"a.py"}'},
        ],
    )
    second = SimpleNamespace(
        id="resp-b",
        model="gpt-4o-mini",
        status="completed",
        usage=SimpleNamespace(input_tokens=4, output_tokens=3),
        output=[{"type": "function_call", "call_id": "call-2", "name": "read_file", "arguments": '{"path":"b.py"}'}],
    )
    fake = FakeResponses(response=first)
    adapter = OpenAIResponsesAdapter(openai_config(tmp_path), client=FakeOpenAIClient(fake))
    adapter.complete(initial_request())
    fake.response = second
    adapter.complete(followup_request())
    fake.response = SimpleNamespace(
        id="resp-c",
        model="gpt-4o-mini",
        status="completed",
        usage=SimpleNamespace(input_tokens=2, output_tokens=1),
        output=[{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}],
    )
    compacted = ModelRequest(
        messages=(
            ModelMessage("system", "compressed system"),
            ModelMessage("user", "inspect"),
            ModelMessage("tool", tool_results=(tool_result("call-1"), tool_result("call-2", content="y"))),
        ),
        tools=(ToolDefinition("read_file", "read", read_schema()),),
        max_output_tokens=4096,
        timeout_seconds=300,
    )
    adapter.complete(compacted)
    outputs = [item for item in fake.calls[2]["input"] if isinstance(item, dict) and item.get("type") == "function_call_output"]
    assert [item["call_id"] for item in outputs] == ["call-1", "call-2"]
    assert len(outputs) == 2
    assert fake.calls[2]["instructions"] == "compressed system"


def test_openai_adapter_explicit_wire_reset_preserves_runtime_identity(tmp_path: Path):
    response = SimpleNamespace(
        id="resp-reset",
        model="gpt-4o-mini",
        status="completed",
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        output=[{"type": "function_call", "call_id": "call-reset", "name": "read_file", "arguments": '{"path":"a.py"}'}],
    )
    fake = FakeResponses(response=response)
    adapter = OpenAIResponsesAdapter(openai_config(tmp_path), client=FakeOpenAIClient(fake))
    adapter.complete(initial_request())
    client, config, identity, actual_model = adapter.client, adapter.config, dict(adapter.identity), adapter.actual_model

    adapter.reset_wire_history()

    assert adapter.client is client
    assert adapter.config is config
    assert adapter.identity == identity
    assert adapter.actual_model == actual_model == "gpt-4o-mini"
    assert adapter._instructions is None
    assert adapter._wire_input == []
    assert adapter._initial_user_added is False
    assert adapter._processed_call_ids == set()
