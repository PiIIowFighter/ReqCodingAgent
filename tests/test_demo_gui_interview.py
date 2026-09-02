from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import Mock
from dataclasses import dataclass

import pytest


# Real ontology slots from frozen baseline-v3
VALID_SLOTS = {
    "goal", "current_behavior_or_symptom", "expected_behavior",
    "target_component", "relevant_symbol_or_api", "affected_consumers",
    "compatibility", "boundary_and_error_semantics", "excluded_scope",
    "acceptance_criteria", "relevant_tests_or_checks"
}


@dataclass
class FakeModelAdapter:
    """Fake model adapter for testing."""
    def __init__(self):
        self.responses = []
        self.call_count = 0
        self.config = Mock(model={"model": "gpt-4o-mini"})

    def add_response(self, tool_name: str, tool_input: dict, call_id: str | None = None, actual_model: str = "gpt-4o-mini"):
        if call_id is None:
            call_id = f"call-{len(self.responses) + 1}"

        from reqagent.model import ModelResponse, NormalizedToolCall

        response = ModelResponse(
            text="",
            tool_calls=(NormalizedToolCall(call_id=call_id, name=tool_name, arguments=tool_input),),
            finish_reason="tool_calls",
            actual_model=actual_model
        )
        self.responses.append(response)

    def add_error(self, error_message: str, *, category: str | None = None):
        """Add an error response for testing retry logic."""
        if category is None:
            self.responses.append(Exception(error_message))
        else:
            from reqagent.model import ModelError
            self.responses.append(ModelError(category, error_message))

    def complete(self, request):
        if self.call_count >= len(self.responses):
            raise ValueError("No more fake responses available")
        response = self.responses[self.call_count]
        self.call_count += 1

        # If it's an exception, raise it
        if isinstance(response, Exception):
            raise response

        return response


def test_interview_starts_with_first_question():
    """Test that interview generates first question with real slots."""
    from demo_gui.interview import InterviewSession

    adapter = FakeModelAdapter()
    adapter.add_response("ask_clarification", {
        "question": "What is the main goal of this change?",
        "slot_ids": ["goal"],
        "selection_reason": "Need to understand the primary objective"
    }, "call-1")

    session = InterviewSession("生成一个股票搜索网站", adapter, "test-v1")
    turn = session.generate_next_question()

    assert turn is not None
    assert turn.turn_id == "call-1"
    assert turn.selected_slot_ids == ["goal"]
    assert len(session.turns) == 1
    assert session.actual_models == ["gpt-4o-mini"]


def test_interview_error_exposes_only_safe_model_category_and_status():
    from demo_gui.interview import InterviewSession
    from reqagent.model import ModelError

    class RejectingAdapter:
        def complete(self, request):
            raise ModelError("request", "response body contains secret", status_code=400)

    session = InterviewSession("需要细化的任务", RejectingAdapter(), "test")
    with pytest.raises(ValueError, match=r"request, HTTP 400") as caught:
        session.generate_next_question()
    assert "secret" not in str(caught.value)


def test_interview_requires_at_least_two_turns():
    """Test that interview cannot finish before minimum turns."""
    from demo_gui.interview import InterviewSession

    adapter = FakeModelAdapter()
    adapter.add_response("ask_clarification", {
        "question": "What is the goal?",
        "slot_ids": ["goal"],
        "selection_reason": "Need goal"
    }, "call-1")

    session = InterviewSession("测试任务", adapter, "test-v1")
    turn1 = session.generate_next_question()

    # Try to finish immediately - finish_interview tool not available yet
    # The tool won't even be provided, so model can't call it
    # This tests that the state machine correctly limits available tools

    # After only 1 turn, finish_interview should not be in allowed tools
    allowed = session._allowed_tool_names()
    assert "finish_interview" not in allowed
    assert "ask_clarification" in allowed


def test_interview_finishes_after_max_turns():
    """Test that interview must finish by turn 3."""
    from demo_gui.interview import InterviewSession

    adapter = FakeModelAdapter()

    # Turn 1
    adapter.add_response("ask_clarification", {
        "question": "What is the goal?",
        "slot_ids": ["goal"],
        "selection_reason": "Need goal"
    }, "call-1")

    session = InterviewSession("测试任务", adapter, "test-v1")
    turn1 = session.generate_next_question()
    assert len(session.turns) == 1

    # Answer 1 -> Turn 2
    adapter.add_response("ask_clarification", {
        "question": "What is the target component?",
        "slot_ids": ["target_component"],
        "selection_reason": "Need to know what to modify"
    }, "call-2")

    turn2 = session.submit_answer("call-1", "答案1")
    assert turn2 is not None
    assert len(session.turns) == 2

    # Answer 2 -> Turn 3
    adapter.add_response("ask_clarification", {
        "question": "What are acceptance criteria?",
        "slot_ids": ["acceptance_criteria"],
        "selection_reason": "Need validation"
    }, "call-3")

    turn3 = session.submit_answer("call-2", "答案2")
    assert turn3 is not None
    assert len(session.turns) == 3

    # Answer 3 -> Must finish (finish_interview is now the only available tool)
    adapter.add_response("finish_interview", {
        "refined_summary": "细化后的任务",
        "requirements": ["需求1", "需求2"],
        "acceptance_criteria": ["标准1"],
        "constraints": ["约束1"],
        "excluded_scope": ["排除1"],
        "assumptions": ["假设1"],
        "unresolved_items": ["未解决1"],
        "slot_states": {
            "goal": {"state": "confirmed", "value": "值1", "evidence": "答案1"},
            "target_component": {"state": "confirmed", "value": "值2", "evidence": "答案2"},
            "acceptance_criteria": {"state": "confirmed", "value": "值3", "evidence": "答案3"}
        }
    })

    result = session.submit_answer("call-3", "答案3")
    assert result is None  # Signals completion
    assert session.baseline is not None
    assert session.baseline.refined_summary == "细化后的任务"
    assert len(session.baseline.requirements) == 2
    assert len(session.actual_models) == 4


def test_interview_rejects_invalid_slots():
    """Test that invalid slot IDs are rejected."""
    from demo_gui.interview import InterviewSession

    adapter = FakeModelAdapter()
    adapter.add_response("ask_clarification", {
        "question": "What technology?",
        "slot_ids": ["technology_stack"],  # Invalid slot
        "selection_reason": "Need tech"
    }, "call-1")

    session = InterviewSession("测试任务", adapter, "test-v1")

    with pytest.raises(ValueError, match="Invalid slot IDs.*technology_stack"):
        session.generate_next_question()


def test_interview_rejects_category_name_as_slot():
    """Test that category names like 'constraints' are rejected as slot IDs."""
    from demo_gui.interview import InterviewSession

    adapter = FakeModelAdapter()
    adapter.add_response("ask_clarification", {
        "question": "What are the constraints?",
        "slot_ids": ["constraints"],  # Category name, not a valid slot
        "selection_reason": "Need constraints"
    }, "call-1")

    session = InterviewSession("测试任务", adapter, "test-v1")

    with pytest.raises(ValueError, match="Invalid slot IDs.*constraints"):
        session.generate_next_question()


def test_corrective_retry_on_malformed_response():
    """Test that one corrective retry is attempted on malformed response."""
    from demo_gui.interview import InterviewSession

    adapter = FakeModelAdapter()

    # First attempt: error
    adapter.add_error("malformed response: invalid slot_ids", category="malformed_response")

    # Second attempt: success
    adapter.add_response("ask_clarification", {
        "question": "What is the goal?",
        "slot_ids": ["goal"],
        "selection_reason": "Need to understand objective"
    }, "call-1")

    session = InterviewSession("测试任务", adapter, "test-v1")
    turn = session.generate_next_question()

    # Should succeed after retry
    assert turn is not None
    assert turn.selected_slot_ids == ["goal"]
    assert adapter.call_count == 2  # Two attempts made


def test_no_retry_on_non_malformed_error():
    """Test that non-malformed errors are not retried."""
    from demo_gui.interview import InterviewSession

    adapter = FakeModelAdapter()
    adapter.add_error("authentication failed")

    session = InterviewSession("测试任务", adapter, "test-v1")

    with pytest.raises(ValueError, match="Interview processing failed"):
        session.generate_next_question()

    assert adapter.call_count == 1  # Only one attempt


def test_max_one_retry():
    """Test that at most one retry is attempted."""
    from demo_gui.interview import InterviewSession

    adapter = FakeModelAdapter()

    # Both attempts fail
    adapter.add_error("malformed response: error 1", category="malformed_response")
    adapter.add_error("malformed response: error 2", category="malformed_response")

    session = InterviewSession("测试任务", adapter, "test-v1")

    with pytest.raises(ValueError, match="Interview processing failed"):
        session.generate_next_question()

    assert adapter.call_count == 2  # Exactly two attempts


def test_fourth_question_prevented():
    """Test that fourth question cannot be generated."""
    from demo_gui.interview import InterviewSession

    adapter = FakeModelAdapter()

    # Complete 3 turns
    adapter.add_response("ask_clarification", {
        "question": "Question 1",
        "slot_ids": ["goal"],
        "selection_reason": "Reason 1"
    }, "call-1")

    adapter.add_response("ask_clarification", {
        "question": "Question 2",
        "slot_ids": ["target_component"],
        "selection_reason": "Reason 2"
    }, "call-2")

    adapter.add_response("ask_clarification", {
        "question": "Question 3",
        "slot_ids": ["acceptance_criteria"],
        "selection_reason": "Reason 3"
    }, "call-3")

    session = InterviewSession("测试任务", adapter, "test-v1")

    # Turn 1
    turn1 = session.generate_next_question()
    assert turn1.turn_id == "call-1"

    # Turn 2
    turn2 = session.submit_answer("call-1", "答案1")
    assert turn2.turn_id == "call-2"

    # Turn 3
    turn3 = session.submit_answer("call-2", "答案2")
    assert turn3.turn_id == "call-3"

    # After 3 turns, ask_clarification should not be allowed
    adapter.add_response("ask_clarification", {
        "question": "Fourth question",
        "slot_ids": ["expected_behavior"],
        "selection_reason": "Should fail"
    }, "call-4")

    with pytest.raises(ValueError, match="Tool ask_clarification not allowed"):
        session.submit_answer("call-3", "答案3")


def test_interview_rejects_duplicate_answer():
    """Test that answering the same turn twice is rejected."""
    from demo_gui.interview import InterviewSession

    adapter = FakeModelAdapter()
    adapter.add_response("ask_clarification", {
        "question": "What is the goal?",
        "slot_ids": ["goal"],
        "selection_reason": "Need goal"
    }, "call-1")

    session = InterviewSession("测试任务", adapter, "test-v1")
    turn = session.generate_next_question()

    adapter.add_response("ask_clarification", {
        "question": "What is the target?",
        "slot_ids": ["target_component"],
        "selection_reason": "Need target"
    }, "call-2")

    session.submit_answer("call-1", "答案1")

    # Try to answer call-1 again
    with pytest.raises(ValueError, match="already answered"):
        session.submit_answer("call-1", "再次答案")


def test_interview_rejects_empty_answer():
    """Test that empty answers are rejected."""
    from demo_gui.interview import InterviewSession

    adapter = FakeModelAdapter()
    adapter.add_response("ask_clarification", {
        "question": "What is the goal?",
        "slot_ids": ["goal"],
        "selection_reason": "Need goal"
    }, "call-1")

    session = InterviewSession("测试任务", adapter, "test-v1")
    session.generate_next_question()

    with pytest.raises(ValueError, match="empty"):
        session.submit_answer("call-1", "   ")


def test_baseline_uses_trusted_original_request():
    """Test that baseline uses session's original request, not model's echo."""
    from demo_gui.interview import InterviewSession

    adapter = FakeModelAdapter()

    # Turn 1
    adapter.add_response("ask_clarification", {
        "question": "What is the goal?",
        "slot_ids": ["goal"],
        "selection_reason": "Need goal"
    }, "call-1")

    session = InterviewSession("原始真实请求", adapter, "test-v1")
    session.generate_next_question()

    # Turn 2 and finish
    adapter.add_response("ask_clarification", {
        "question": "What is the target?",
        "slot_ids": ["target_component"],
        "selection_reason": "Need target"
    }, "call-2")

    session.submit_answer("call-1", "答案1")

    # Finish with different original_request (should be ignored)
    adapter.add_response("finish_interview", {
        "refined_summary": "摘要",
        "requirements": ["需求1"],
        "acceptance_criteria": ["标准1"],
        "constraints": [],
        "excluded_scope": [],
        "assumptions": [],
        "unresolved_items": [],
        "slot_states": {}
    })

    session.submit_answer("call-2", "答案2")

    # Baseline should use session's original_request
    assert session.baseline.original_request == "原始真实请求"


def test_slot_updates_backfilled():
    """Test that slot_updates are backfilled after baseline formation."""
    from demo_gui.interview import InterviewSession

    adapter = FakeModelAdapter()

    # Turn 1
    adapter.add_response("ask_clarification", {
        "question": "What is the goal?",
        "slot_ids": ["goal", "expected_behavior"],
        "selection_reason": "Need goal"
    }, "call-1")

    session = InterviewSession("测试", adapter, "test-v1")
    session.generate_next_question()

    # Turn 2 and finish
    adapter.add_response("ask_clarification", {
        "question": "What is the target?",
        "slot_ids": ["target_component"],
        "selection_reason": "Need target"
    }, "call-2")

    session.submit_answer("call-1", "答案1")

    # Now finish
    adapter.add_response("finish_interview", {
        "refined_summary": "摘要",
        "requirements": ["需求1"],
        "acceptance_criteria": ["标准1"],
        "constraints": [],
        "excluded_scope": [],
        "assumptions": [],
        "unresolved_items": [],
        "slot_states": {
            "goal": {"state": "confirmed", "value": "目标值", "evidence": "用户回答"},
            "expected_behavior": {"state": "confirmed", "value": "行为值", "evidence": "用户回答"}
        }
    })

    session.submit_answer("call-2", "答案2")

    # Check that turn 1 has slot_updates backfilled
    assert "goal" in session.turns[0].slot_updates
    assert "expected_behavior" in session.turns[0].slot_updates
    assert session.turns[0].slot_updates["goal"]["state"] == "confirmed"


def test_baseline_to_task_description():
    """Test baseline conversion to task description."""
    from demo_gui.interview import RequirementBaseline

    baseline = RequirementBaseline(
        original_request="生成一个股票搜索网站",
        refined_summary="使用HTML/CSS/JS创建本地股票搜索网站",
        requirements=["支持按代码搜索", "显示股票信息"],
        acceptance_criteria=["搜索功能正常", "结果显示正确"],
        constraints=["仅使用原生技术"],
        excluded_scope=["不需要真实API"],
        assumptions=["使用模拟数据"],
        unresolved_items=["数据更新频率未确定"],
        slot_states={}
    )

    task_desc = baseline.to_task_description()

    assert "# Original Request" in task_desc
    assert "生成一个股票搜索网站" in task_desc
    assert "# Requirements" in task_desc
    assert "支持按代码搜索" in task_desc
    assert "# Acceptance Criteria" in task_desc
    assert "搜索功能正常" in task_desc


def test_sanitization_rejects_api_keys():
    """Test that suspected API keys in answers are rejected."""
    from demo_gui.interview import InterviewSession

    adapter = FakeModelAdapter()
    adapter.add_response("ask_clarification", {
        "question": "What is the goal?",
        "slot_ids": ["goal"],
        "selection_reason": "Need goal"
    }, "call-1")

    session = InterviewSession("测试任务", adapter, "test-v1")
    session.generate_next_question()

    # Try to submit answer with API key
    with pytest.raises(ValueError, match="API key"):
        fake_prefix = "s" + "k-"
        session.submit_answer("call-1", "Use API key " + fake_prefix + "1234567890abcdefghijklmnopqrstuvwxyz")


def test_sanitization_redacts_paths():
    """Test that absolute paths are redacted from transcript."""
    from demo_gui.interview import InterviewSession

    adapter = FakeModelAdapter()
    adapter.add_response("ask_clarification", {
        "question": "Where is the file C:\\Users\\test\\secret.txt?",
        "slot_ids": ["target_component"],
        "selection_reason": "Need location"
    }, "call-1")

    session = InterviewSession("测试任务", adapter, "test-v1")
    turn = session.generate_next_question()

    # Question should be redacted
    assert "C:\\" not in turn.question
    assert "[redacted]" in turn.question

    adapter.add_response("ask_clarification", {
        "question": "What about /home/user/data?",
        "slot_ids": ["relevant_symbol_or_api"],
        "selection_reason": "Need API"
    }, "call-2")

    turn2 = session.submit_answer("call-1", "It's in D:\\projects\\myapp\\file.txt")

    # Answer should be redacted in transcript
    transcript = session.to_transcript()
    assert "D:\\" not in json.dumps(transcript)
    assert "/home/user" not in json.dumps(transcript)


def test_slot_states_sanitized_recursively():
    """Test that slot_states are sanitized recursively in baseline."""
    from demo_gui.interview import InterviewSession

    adapter = FakeModelAdapter()
    adapter.add_response("ask_clarification", {
        "question": "Question 1?",
        "slot_ids": ["goal"],
        "selection_reason": "Reason"
    }, "call-1")

    session = InterviewSession("测试", adapter, "test-v1")
    session.generate_next_question()

    # Turn 2
    adapter.add_response("ask_clarification", {
        "question": "Question 2?",
        "slot_ids": ["target_component"],
        "selection_reason": "Reason 2"
    }, "call-2")

    session.submit_answer("call-1", "答案1")

    # Finish with slot_states containing paths
    adapter.add_response("finish_interview", {
        "refined_summary": "摘要",
        "requirements": ["需求1"],
        "acceptance_criteria": ["标准1"],
        "constraints": [],
        "excluded_scope": [],
        "assumptions": [],
        "unresolved_items": [],
        "slot_states": {
            "goal": {
                "state": "confirmed",
                "value": "Store in C:\\data\\config.json",
                "evidence": "User said C:\\data"
            }
        }
    })

    session.submit_answer("call-2", "答案2")

    # Check baseline dict has redacted paths
    baseline_dict = session.baseline.to_dict()
    slot_states_json = json.dumps(baseline_dict["slot_states"])
    assert "C:\\" not in slot_states_json
    assert "[redacted]" in slot_states_json


def test_greenfield_task_enters_interview():
    """Test that vague greenfield task triggers interview mode."""
    from reqagent.adaptive import route_task

    decision = route_task("生成一个股票搜索网站")
    assert decision.mode == "refine"


def test_detailed_task_skips_interview():
    """Test that detailed task goes directly to coding."""
    from reqagent.adaptive import route_task

    detailed_task = "In parser.py, update parse(value) so empty strings return None; run test_parser.py."
    decision = route_task(detailed_task)
    assert decision.mode == "fast"
