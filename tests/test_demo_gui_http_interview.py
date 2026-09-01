"""HTTP end-to-end test for interview workflow with dependency injection."""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from unittest import TestCase

from demo_gui.server import TaskManager


class FakeInterviewAdapter:
    """Fake adapter that provides scripted interview responses."""
    def __init__(self):
        self.call_count = 0
        self.config = type('obj', (object,), {'model': {'model': 'gpt-4o-mini'}})()

    def complete(self, request):
        from reqagent.model import ModelResponse, NormalizedToolCall

        # Turn 1: Ask about goal
        if self.call_count == 0:
            self.call_count += 1
            return ModelResponse(
                tool_calls=(NormalizedToolCall(
                    call_id="call-1",
                    name="ask_clarification",
                    arguments={
                        "question": "这个股票搜索网站的主要目标是什么？",
                        "slot_ids": ["goal"],
                        "selection_reason": "需要理解主要目标"
                    }
                ),),
                finish_reason="tool_calls",
                actual_model="gpt-4o-mini"
            )

        # Turn 2: Ask about acceptance criteria
        elif self.call_count == 1:
            self.call_count += 1
            return ModelResponse(
                tool_calls=(NormalizedToolCall(
                    call_id="call-2",
                    name="ask_clarification",
                    arguments={
                        "question": "验收标准是什么？",
                        "slot_ids": ["acceptance_criteria"],
                        "selection_reason": "需要定义成功指标"
                    }
                ),),
                finish_reason="tool_calls",
                actual_model="gpt-4o-mini"
            )

        # Turn 3: Finish interview
        elif self.call_count == 2:
            self.call_count += 1
            return ModelResponse(
                tool_calls=(NormalizedToolCall(
                    call_id="call-3",
                    name="finish_interview",
                    arguments={
                        "refined_summary": "创建一个基于本地模拟数据的股票搜索网站",
                        "requirements": [
                            "支持按代码或名称搜索股票",
                            "显示股票的基本信息和价格",
                            "使用本地模拟数据，无需外部API"
                        ],
                        "acceptance_criteria": [
                            "用户可以在搜索框输入股票代码或名称",
                            "搜索结果准确显示匹配的股票信息",
                            "无匹配结果时显示友好提示"
                        ],
                        "constraints": [
                            "使用原生HTML、CSS、JavaScript",
                            "不依赖外部API或数据源"
                        ],
                        "excluded_scope": [
                            "实时股票数据",
                            "用户认证功能"
                        ],
                        "assumptions": [
                            "使用预定义的模拟股票数据",
                            "深色主题界面"
                        ],
                        "unresolved_items": [],
                        "slot_states": {
                            "goal": {"state": "confirmed", "value": "股票搜索", "evidence": "用户回答1"},
                            "acceptance_criteria": {"state": "confirmed", "value": "搜索和显示", "evidence": "用户回答2"}
                        }
                    }
                ),),
                finish_reason="tool_calls",
                actual_model="gpt-4o-mini"
            )

        raise ValueError("No more responses")


class TestInterviewHTTPWorkflow(TestCase):
    """Test complete interview workflow through HTTP-like API with fake adapter."""

    def test_complete_interview_workflow(self):
        """Test complete workflow: vague task -> interview -> confirmation -> ready for coding."""
        with tempfile.TemporaryDirectory() as workspace:
            workspace_path = Path(workspace)

            config_path = Path.cwd() / "configs/agent/demo-openai.json"
            if not config_path.exists():
                self.skipTest("Demo config not found")

            with tempfile.TemporaryDirectory() as artifact_dir:
                # Use interview_adapter_factory for dependency injection
                manager = TaskManager(
                    workspace=workspace_path,
                    config=config_path,
                    artifact_root=Path(artifact_dir),
                    python="python",
                    project_root=Path.cwd(),
                    in_place=False,
                    interview_adapter_factory=FakeInterviewAdapter
                )

                # Step 1: Submit vague task
                record = manager.start("生成一个股票搜索网站")

                # Wait for first question
                for _ in range(20):
                    time.sleep(0.1)
                    if record.status == "awaiting_user":
                        break

                self.assertEqual(record.status, "awaiting_user")
                self.assertEqual(record.route_mode, "refine")
                self.assertIsNotNone(record.current_turn)
                self.assertEqual(record.current_turn.selected_slot_ids, ["goal"])
                self.assertIn("目标", record.current_turn.question)

                # Verify workspace empty and no process
                self.assertEqual(len(list(workspace_path.iterdir())), 0)
                self.assertIsNone(record.process)

                turn1_id = record.current_turn.turn_id

                # Step 2: Submit first answer
                result = manager.submit_answer(
                    record.id,
                    turn1_id,
                    "允许用户搜索股票信息，显示价格和涨跌幅"
                )

                # Wait for second question
                for _ in range(20):
                    time.sleep(0.1)
                    if record.status == "awaiting_user" and record.current_turn.turn_id != turn1_id:
                        break

                self.assertEqual(result["status"], "interviewing")
                self.assertEqual(record.status, "awaiting_user")
                self.assertEqual(len(record.interview_session.turns), 2)
                self.assertEqual(record.current_turn.selected_slot_ids, ["acceptance_criteria"])

                # Verify workspace still empty
                self.assertEqual(len(list(workspace_path.iterdir())), 0)

                turn2_id = record.current_turn.turn_id

                # Test: Cannot answer old turn
                with self.assertRaises(ValueError) as cm:
                    manager.submit_answer(record.id, turn1_id, "重复答案")
                self.assertIn("stale turn_id", str(cm.exception))

                # Step 3: Submit second answer
                result = manager.submit_answer(
                    record.id,
                    turn2_id,
                    "搜索功能正常，结果显示正确，无匹配时有提示"
                )

                # Wait for baseline
                for _ in range(20):
                    time.sleep(0.1)
                    if record.status == "awaiting_confirmation":
                        break

                self.assertEqual(record.status, "awaiting_confirmation")
                self.assertIsNotNone(record.baseline)
                self.assertEqual(record.baseline.original_request, "生成一个股票搜索网站")
                self.assertIn("股票", record.baseline.refined_summary)
                self.assertGreaterEqual(len(record.baseline.requirements), 3)

                # Verify workspace STILL empty before confirmation
                self.assertEqual(len(list(workspace_path.iterdir())), 0)
                self.assertIsNone(record.process)

                # Verify baseline not confirmed yet
                self.assertIsNone(record.baseline.confirmed_at)

                # Test: Cannot confirm twice
                result = manager.confirm_baseline(record.id)
                self.assertIsNotNone(record.baseline.confirmed_at)

                # After confirmation, status changes (likely to running or failed)
                # Attempting to confirm again should fail
                with self.assertRaises(ValueError) as cm:
                    manager.confirm_baseline(record.id)
                # Error message depends on current status
                self.assertTrue("not awaiting confirmation" in str(cm.exception) or "already" in str(cm.exception))

                # Verify artifacts saved
                interview_dir = Path(artifact_dir) / f"interview-{record.id}"
                self.assertTrue(interview_dir.exists())

                transcript_path = interview_dir / "interview-transcript.json"
                baseline_path = interview_dir / "confirmed-requirement-baseline.json"
                task_file_path = interview_dir / "final-task.txt"

                self.assertTrue(transcript_path.exists())
                self.assertTrue(baseline_path.exists())
                self.assertTrue(task_file_path.exists())

                # Verify transcript
                transcript = json.loads(transcript_path.read_text(encoding='utf-8'))
                self.assertEqual(transcript["original_request"], "生成一个股票搜索网站")
                self.assertEqual(len(transcript["turns"]), 2)
                self.assertTrue(transcript["completed"])

                # Verify no sensitive data
                transcript_text = json.dumps(transcript)
                self.assertNotIn("C:", transcript_text)
                self.assertNotIn("D:", transcript_text)
                self.assertNotIn("/home/", transcript_text)
                self.assertNotIn("reasoning", transcript_text)
                self.assertNotIn("encrypted_content", transcript_text)

                # Verify baseline
                baseline = json.loads(baseline_path.read_text(encoding='utf-8'))
                self.assertEqual(baseline["configured_model"], "gpt-4o-mini")
                self.assertEqual(baseline["actual_models"], ["gpt-4o-mini", "gpt-4o-mini", "gpt-4o-mini"])
                self.assertIsNotNone(baseline["confirmed_at"])

                # Verify task file
                task_text = task_file_path.read_text(encoding='utf-8')
                self.assertIn("# Original Request", task_text)
                self.assertIn("# Requirements", task_text)
                self.assertIn("# Acceptance Criteria", task_text)

                print("PASS: Complete HTTP interview workflow")


if __name__ == "__main__":
    import unittest
    unittest.main()
