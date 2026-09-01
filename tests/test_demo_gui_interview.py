from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from demo_gui.interview import InterviewSession, RequirementBaseline


class FakeToolCall:
    """Fake tool call for testing."""
    def __init__(self, name: str, arguments: str, call_id: str):
        self.type = "function_call"
        self.name = name
        self.arguments = arguments
        self.call_id = call_id


class FakeResponse:
    """Fake OpenAI response for testing."""
    def __init__(self, tool_name: str, tool_input: dict, call_id: str = "test-call-1"):
        self.model = "gpt-4o-mini"
        self.output = [FakeToolCall(tool_name, json.dumps(tool_input), call_id)]


class FakeClient:
    """Fake OpenAI client for testing."""
    def __init__(self):
        self.responses = []
        self.call_count = 0

    def add_response(self, tool_name: str, tool_input: dict, call_id: str | None = None):
        if call_id is None:
            call_id = f"call-{len(self.responses) + 1}"
        self.responses.append(FakeResponse(tool_name, tool_input, call_id))

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        if self.call_count >= len(self.responses):
            raise ValueError("No more fake responses available")
        response = self.responses[self.call_count]
        self.call_count += 1
        return response


def test_interview_starts_with_first_question():
    """Test that interview generates first question."""
    client = FakeClient()
    client.add_response("ask_clarification", {
        "question": "使用什么技术栈实现网站？",
        "slot_ids": ["technology_stack"],
        "selection_reason": "需要确定技术选型"
    }, "call-1")

    session = InterviewSession("生成一个股票搜索网站", client, "gpt-4o-mini", "test-v1")
    turn = session.generate_next_question()

    assert turn is not None
    assert turn.turn_id == "call-1"
    assert "技术栈" in turn.question
    assert "technology_stack" in turn.selected_slot_ids
    assert len(session.turns) == 1


def test_interview_requires_at_least_two_turns():
    """Test that interview cannot finish before minimum turns."""
    client = FakeClient()
    client.add_response("ask_clarification", {
        "question": "第一个问题",
        "slot_ids": ["slot1"],
        "selection_reason": "原因1"
    }, "call-1")

    session = InterviewSession("测试任务", client, "gpt-4o-mini", "test-v1")
    turn1 = session.generate_next_question()

    # Try to finish immediately - should fail
    client.add_response("finish_interview", {
        "original_request": "测试任务",
        "refined_summary": "测试",
        "requirements": ["req1"],
        "acceptance_criteria": ["ac1"],
        "constraints": [],
        "excluded_scope": [],
        "assumptions": [],
        "unresolved_items": [],
        "slot_states": {}
    })

    with pytest.raises(ValueError, match="Cannot finish interview before 2 turns"):
        session.submit_answer("call-1", "答案1")


def test_interview_finishes_after_max_turns():
    """Test that interview must finish by turn 3."""
    client = FakeClient()

    # Turn 1
    client.add_response("ask_clarification", {
        "question": "问题1",
        "slot_ids": ["slot1"],
        "selection_reason": "原因1"
    }, "call-1")

    session = InterviewSession("测试任务", client, "gpt-4o-mini", "test-v1")
    turn1 = session.generate_next_question()

    # Answer 1 -> Turn 2
    client.add_response("ask_clarification", {
        "question": "问题2",
        "slot_ids": ["slot2"],
        "selection_reason": "原因2"
    }, "call-2")

    turn2 = session.submit_answer("call-1", "答案1")
    assert turn2 is not None
    assert len(session.turns) == 2

    # Answer 2 -> Turn 3
    client.add_response("ask_clarification", {
        "question": "问题3",
        "slot_ids": ["slot3"],
        "selection_reason": "原因3"
    }, "call-3")

    turn3 = session.submit_answer("call-2", "答案2")
    assert turn3 is not None
    assert len(session.turns) == 3

    # Answer 3 -> Must finish (force_finish message is injected automatically)
    client.add_response("finish_interview", {
        "original_request": "测试任务",
        "refined_summary": "细化后的任务",
        "requirements": ["需求1", "需求2"],
        "acceptance_criteria": ["标准1"],
        "constraints": ["约束1"],
        "excluded_scope": ["排除1"],
        "assumptions": ["假设1"],
        "unresolved_items": ["未解决1"],
        "slot_states": {
            "slot1": {"state": "confirmed", "value": "值1", "evidence": "答案1"},
            "slot2": {"state": "confirmed", "value": "值2", "evidence": "答案2"},
            "slot3": {"state": "confirmed", "value": "值3", "evidence": "答案3"}
        }
    })

    result = session.submit_answer("call-3", "答案3")
    assert result is None  # Signals completion
    assert session.baseline is not None
    assert session.baseline.refined_summary == "细化后的任务"
    assert len(session.baseline.requirements) == 2


def test_interview_rejects_duplicate_answer():
    """Test that answering the same turn twice is rejected."""
    client = FakeClient()
    client.add_response("ask_clarification", {
        "question": "问题1",
        "slot_ids": ["slot1"],
        "selection_reason": "原因1"
    }, "call-1")

    session = InterviewSession("测试任务", client, "gpt-4o-mini", "test-v1")
    turn = session.generate_next_question()

    client.add_response("ask_clarification", {
        "question": "问题2",
        "slot_ids": ["slot2"],
        "selection_reason": "原因2"
    }, "call-2")

    session.submit_answer("call-1", "答案1")

    # Try to answer call-1 again
    with pytest.raises(ValueError, match="already answered"):
        session.submit_answer("call-1", "再次答案")


def test_interview_rejects_invalid_turn_id():
    """Test that invalid turn_id is rejected."""
    client = FakeClient()
    client.add_response("ask_clarification", {
        "question": "问题1",
        "slot_ids": ["slot1"],
        "selection_reason": "原因1"
    }, "call-1")

    session = InterviewSession("测试任务", client, "gpt-4o-mini", "test-v1")
    session.generate_next_question()

    with pytest.raises(ValueError, match="Unknown turn_id"):
        session.submit_answer("invalid-id", "答案")


def test_baseline_to_task_description():
    """Test baseline conversion to task description."""
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
    assert "# Constraints" in task_desc
    assert "# Excluded Scope" in task_desc
    assert "# Assumptions" in task_desc
    assert "# Unresolved" in task_desc


def test_interview_transcript_structure():
    """Test that transcript has correct structure."""
    client = FakeClient()
    client.add_response("ask_clarification", {
        "question": "问题1",
        "slot_ids": ["slot1"],
        "selection_reason": "原因1"
    }, "call-1")

    session = InterviewSession("测试任务", client, "gpt-4o-mini", "test-v1")
    session.generate_next_question()

    client.add_response("ask_clarification", {
        "question": "问题2",
        "slot_ids": ["slot2"],
        "selection_reason": "原因2"
    }, "call-2")

    session.submit_answer("call-1", "答案1")

    transcript = session.to_transcript()

    assert transcript["original_request"] == "测试任务"
    assert transcript["ontology_version"] == "test-v1"
    assert len(transcript["turns"]) == 2
    assert transcript["turns"][0]["question"] == "问题1"
    assert transcript["turns"][0]["answer"] == "答案1"
    assert transcript["turns"][1]["answer"] is None  # Not yet answered
    assert "baseline" in transcript
    assert transcript["completed"] is False


def test_greenfield_task_enters_interview():
    """Test that vague greenfield task triggers interview mode."""
    from reqagent.adaptive import route_task

    decision = route_task("生成一个股票搜索网站")
    assert decision.mode == "refine"


def test_detailed_task_skips_interview():
    """Test that detailed task goes directly to coding."""
    from reqagent.adaptive import route_task

    detailed_task = "Create index.html with search form. Fetch /api/stock?symbol=X. Display in #output. Test manually."
    decision = route_task(detailed_task)
    assert decision.mode == "fast"


def test_interview_no_absolute_paths_in_transcript():
    """Test that transcript does not contain absolute paths."""
    client = FakeClient()
    client.add_response("ask_clarification", {
        "question": "问题1",
        "slot_ids": ["slot1"],
        "selection_reason": "原因1"
    }, "call-1")

    session = InterviewSession("测试任务", client, "gpt-4o-mini", "test-v1")
    session.generate_next_question()

    client.add_response("ask_clarification", {
        "question": "问题2",
        "slot_ids": ["slot2"],
        "selection_reason": "原因2"
    }, "call-2")

    session.submit_answer("call-1", "答案1")

    transcript_json = json.dumps(session.to_transcript())

    # Should not contain Windows or Unix absolute paths
    assert "C:\\" not in transcript_json
    assert "D:\\" not in transcript_json
    assert not any(f"/{part}/" in transcript_json for part in ["home", "users", "root", "opt"])


def test_interview_session_preserves_actual_model():
    """Test that actual model from response is preserved."""
    client = FakeClient()
    client.add_response("ask_clarification", {
        "question": "问题1",
        "slot_ids": ["slot1"],
        "selection_reason": "原因1"
    }, "call-1")

    session = InterviewSession("测试任务", client, "gpt-4o-mini", "test-v1")
    session.generate_next_question()

    assert session.actual_model == "gpt-4o-mini"
