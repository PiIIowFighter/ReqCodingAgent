"""Tests for interview corrective retry with real OpenAIResponsesAdapter."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest


ROOT = Path(__file__).resolve().parents[1]


class FakeResponsesClient:
    def __init__(self):
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            arguments = {
                "question": "What are the constraints?",
                "slot_ids": ["constraints"],
                "selection_reason": "Need constraints",
            }
            request_id = "resp-1"
        else:
            arguments = {
                "question": "What is the goal?",
                "slot_ids": ["goal"],
                "selection_reason": "Need to understand goal",
            }
            request_id = "resp-2"
        return SimpleNamespace(
            id=request_id,
            model="gpt-4o-mini",
            status="completed",
            usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
            output=[{
                "type": "function_call",
                "call_id": "call-1",
                "name": "ask_clarification",
                "arguments": json.dumps(arguments),
            }],
        )


class FakeOpenAIClient:
    def __init__(self, responses):
        self.responses = responses


def real_adapter(tmp_path, monkeypatch, fake):
    from reqagent.config import AgentConfig
    from reqagent.openai_responses import OpenAIResponsesAdapter

    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
    raw = json.loads((ROOT / "configs/agent/demo-openai.json").read_text(encoding="utf-8"))
    config_path = tmp_path / "agent.json"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    return OpenAIResponsesAdapter(AgentConfig.load(config_path), client=FakeOpenAIClient(fake))


def test_corrective_retry_with_real_adapter_malformed_response(tmp_path, monkeypatch):
    """Test that corrective retry works with real ModelError.category from adapter."""
    from demo_gui.interview import InterviewSession
    fake_client = FakeResponsesClient()
    adapter = real_adapter(tmp_path, monkeypatch, fake_client)
    session = InterviewSession("测试任务", adapter, "test-v1")

    turn = session.generate_next_question()

    assert len(fake_client.calls) == 2
    assert turn is not None
    assert turn.selected_slot_ids == ["goal"]
    assert len(session.turns) == 1
    assert session.actual_models == ["gpt-4o-mini"]


def test_corrective_retry_preserves_wire_history(tmp_path, monkeypatch):
    """Test that corrective retry preserves Responses wire history."""
    from demo_gui.interview import InterviewSession
    fake_client = FakeResponsesClient()
    adapter = real_adapter(tmp_path, monkeypatch, fake_client)
    session = InterviewSession("测试", adapter, "test-v1")
    initial_message_count = len(session.messages)

    session.generate_next_question()

    assert len(fake_client.calls) == 2
    first, second = fake_client.calls
    assert second["input"] == first["input"]
    assert len(second["input"]) == 1
    assert second["input"][0]["role"] == "user"
    assert "Original user request: 测试" in second["input"][0]["content"]
    assert "previous response had a schema error" not in first["instructions"]
    assert "previous response had a schema error" in second["instructions"]
    assert "Valid slot IDs are:" in second["instructions"]
    assert len(session.messages) == initial_message_count + 1
    assert all("previous response had a schema error" not in message.text for message in session.messages)


def test_non_malformed_error_not_retried():
    """Test that non-malformed ModelError is not retried."""
    from demo_gui.interview import InterviewSession
    from reqagent.model import ModelError

    class FakeAdapter:
        def __init__(self):
            self.call_count = 0
            self.config = Mock(model={"model": "gpt-4o-mini"})

        def complete(self, request):
            self.call_count += 1
            # Simulate authentication error
            raise ModelError(
                message="Authentication failed",
                category="authentication",
                retryable=False
            )

    adapter = FakeAdapter()
    session = InterviewSession("测试", adapter, "test-v1")

    # Should fail without retry
    with pytest.raises(ValueError, match="Interview processing failed"):
        session.generate_next_question()

    # Only one attempt should have been made
    assert adapter.call_count == 1


def test_session_state_not_modified_on_failure():
    """Test that Session state is not modified when validation fails."""
    from demo_gui.interview import InterviewSession

    class FakeAdapter:
        def __init__(self):
            self.config = Mock(model={"model": "gpt-4o-mini"})

        def complete(self, request):
            from reqagent.model import ModelResponse, NormalizedToolCall

            # Return invalid response (wrong tool name)
            return ModelResponse(
                tool_calls=(NormalizedToolCall(
                    call_id="call-1",
                    name="invalid_tool",  # Not allowed
                    arguments={"question": "What?", "slot_ids": ["goal"], "selection_reason": "Reason"}
                ),),
                finish_reason="tool_calls",
                actual_model="gpt-4o-mini"
            )

    adapter = FakeAdapter()
    session = InterviewSession("测试", adapter, "test-v1")

    initial_turns = len(session.turns)
    initial_messages = len(session.messages)
    initial_call_ids = len(session.used_call_ids)
    initial_actual_models = len(session.actual_models)

    # Should fail validation
    with pytest.raises(ValueError, match="not allowed"):
        session.generate_next_question()

    # Session state should be unchanged
    assert len(session.turns) == initial_turns
    assert len(session.messages) == initial_messages
    assert len(session.used_call_ids) == initial_call_ids
    assert len(session.actual_models) == initial_actual_models
    assert session.baseline is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
