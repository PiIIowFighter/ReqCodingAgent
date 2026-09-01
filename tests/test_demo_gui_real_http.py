"""Real HTTP end-to-end test for interview workflow."""
from __future__ import annotations

import json
import tempfile
import threading
import time
from http.client import HTTPConnection
from pathlib import Path
from unittest import TestCase
from urllib.parse import urlencode


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


class FakeCodingRunner:
    """Fake coding runner that generates scripted patch without subprocess."""
    def __init__(self, workspace: Path, task_file: Path, artifact_root: Path):
        self.workspace = workspace
        self.task_file = task_file
        self.artifact_root = artifact_root
        self.run_id = None

    def run(self) -> dict:
        """Generate fake successful result with patch."""
        # Verify task file exists and contains baseline
        task_content = self.task_file.read_text(encoding='utf-8')
        assert "# Original Request" in task_content
        assert "# Requirements" in task_content

        # Create fake run directory
        import hashlib
        import time
        timestamp = time.strftime("%Y%m%dT%H%M%S") + f"{int(time.time() * 1000) % 1000:03d}Z"
        run_hash = hashlib.sha256(task_content.encode()).hexdigest()[:4]
        self.run_id = f"{timestamp}-fake-{run_hash}"

        run_dir = self.artifact_root / self.run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        # Create fake patch
        fake_patch = """--- /dev/null
+++ b/index.html
@@ -0,0 +1,10 @@
+<!DOCTYPE html>
+<html>
+<head>
+  <title>Stock Search</title>
+</head>
+<body>
+  <h1>Stock Search</h1>
+  <input type="text" id="search" placeholder="Enter stock code or name">
+</body>
+</html>
"""
        patch_path = run_dir / "agent.patch"
        patch_path.write_text(fake_patch, encoding='utf-8')

        # Create fake result.json
        result = {
            "run_id": self.run_id,
            "stop_reason": "submitted",
            "submitted": {
                "summary": "Created stock search website",
                "limitations": "Uses mock data only"
            },
            "patch": {
                "files": 1,
                "additions": 10,
                "deletions": 0,
                "bytes": len(fake_patch.encode())
            },
            "steps": 5,
            "tool_calls": 3,
            "usage": {
                "input_tokens": 1500,
                "output_tokens": 500,
                "total_tokens": 2000
            }
        }

        result_path = run_dir / "result.json"
        result_path.write_text(json.dumps(result, indent=2), encoding='utf-8')

        # Write to workspace
        index_html = self.workspace / "index.html"
        index_html.write_text("""<!DOCTYPE html>
<html>
<head>
  <title>Stock Search</title>
</head>
<body>
  <h1>Stock Search</h1>
  <input type="text" id="search" placeholder="Enter stock code or name">
</body>
</html>
""", encoding='utf-8')

        return result


class TestRealHTTPInterviewWorkflow(TestCase):
    """Test complete interview workflow through real HTTP endpoints."""

    def test_complete_http_workflow(self):
        """Test vague task -> interview -> confirmation -> coding via HTTP."""
        import socket
        from demo_gui.server import create_server, TaskManager

        # Find free port
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]

        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace_path = Path(workspace_dir)

            with tempfile.TemporaryDirectory() as artifact_dir:
                artifact_path = Path(artifact_dir)
                config_path = Path.cwd() / "configs/agent/demo-openai.json"

                if not config_path.exists():
                    self.skipTest("Demo config not found")

                # Create server with fake interview adapter
                # We need to monkey-patch TaskManager to inject fake adapter
                original_task_manager_init = TaskManager.__init__

                def patched_init(self, workspace, config, artifact_root, *, project_root=None, python="python", in_place=True):
                    # Call original init
                    original_task_manager_init(
                        self, workspace, config, artifact_root,
                        project_root=project_root or Path.cwd(),
                        python=python,
                        in_place=in_place,
                        interview_adapter_factory=FakeInterviewAdapter
                    )

                    # Replace _run_coding_agent with fake runner
                    original_run_coding_agent = self._run_coding_agent

                    def fake_run_coding_agent(record, task_file=None):
                        """Fake coding agent runner without subprocess."""
                        import time

                        def _now():
                            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

                        with self.lock:
                            record.status = "running"
                            record.started_at = _now() if not record.started_at else record.started_at

                        try:
                            # Read task file
                            if task_file is None:
                                interview_dir = artifact_path / f"interview-{record.id}"
                                task_file = interview_dir / "final-task.txt"

                            runner = FakeCodingRunner(workspace_path, task_file, artifact_path)
                            result = runner.run()

                            with self.lock:
                                record.run_id = result["run_id"]
                                record.result = result
                                from demo_gui.server import status_for_stop_reason
                                record.status = status_for_stop_reason(result.get("stop_reason"))
                                record.finished_at = _now()
                                if self.active_id == record.id:
                                    self.active_id = None
                        except Exception as exc:
                            with self.lock:
                                record.status = "failed"
                                record.error = str(exc)
                                record.finished_at = _now()
                                if self.active_id == record.id:
                                    self.active_id = None

                    self._run_coding_agent = fake_run_coding_agent

                TaskManager.__init__ = patched_init

                try:
                    # Create server
                    server = create_server(
                        host='127.0.0.1',
                        port=port,
                        workspace=workspace_path,
                        config=config_path,
                        artifact_root=artifact_path,
                        python="python",
                        project_root=Path.cwd(),
                        in_place=False
                    )

                    # Start server in background
                    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
                    server_thread.start()

                    # Wait for server
                    time.sleep(0.2)

                    try:
                        conn = HTTPConnection('127.0.0.1', port, timeout=10)

                        # Step 1: POST /api/tasks
                        task_data = json.dumps({"task": "生成一个股票搜索网站"})
                        conn.request('POST', '/api/tasks', body=task_data,
                                    headers={'Content-Type': 'application/json'})
                        response = conn.getresponse()
                        self.assertEqual(response.status, 202)
                        data = json.loads(response.read())

                        task_id = data["id"]
                        self.assertEqual(data["route_mode"], "refine")

                        # Wait for first question
                        for _ in range(50):
                            conn.request('GET', f'/api/tasks/{task_id}')
                            response = conn.getresponse()
                            data = json.loads(response.read())
                            if data["status"] == "awaiting_user":
                                break
                            time.sleep(0.1)
                        else:
                            self.fail(f"Timeout waiting for first question. Final status: {data.get('status')}")

                        self.assertEqual(data["status"], "awaiting_user")
                        self.assertIn("current_question", data, f"Missing current_question in response: {data}")
                        self.assertIn("目标", data["current_question"]["question"])
                        turn1_id = data["current_question"]["turn_id"]

                        # Verify workspace empty
                        self.assertEqual(len(list(workspace_path.iterdir())), 0)

                        # Step 2: POST answer 1
                        answer1_data = json.dumps({
                            "turn_id": turn1_id,
                            "answer": "允许用户搜索股票信息，显示价格和涨跌幅"
                        })
                        conn.request('POST', f'/api/tasks/{task_id}/answer', body=answer1_data,
                                    headers={'Content-Type': 'application/json'})
                        response = conn.getresponse()
                        self.assertEqual(response.status, 202)
                        data = json.loads(response.read())
                        self.assertEqual(data["status"], "interviewing")

                        # Wait for second question
                        for _ in range(50):
                            conn.request('GET', f'/api/tasks/{task_id}')
                            response = conn.getresponse()
                            data = json.loads(response.read())
                            if data["status"] == "awaiting_user" and data.get("current_question", {}).get("turn_id") != turn1_id:
                                break
                            time.sleep(0.1)

                        self.assertEqual(data["status"], "awaiting_user")
                        turn2_id = data["current_question"]["turn_id"]

                        # Verify workspace still empty
                        self.assertEqual(len(list(workspace_path.iterdir())), 0)

                        # Step 3: Try to answer old turn (should fail)
                        old_answer_data = json.dumps({
                            "turn_id": turn1_id,
                            "answer": "重复答案"
                        })
                        conn.request('POST', f'/api/tasks/{task_id}/answer', body=old_answer_data,
                                    headers={'Content-Type': 'application/json'})
                        response = conn.getresponse()
                        self.assertEqual(response.status, 400)

                        # Step 4: POST answer 2
                        answer2_data = json.dumps({
                            "turn_id": turn2_id,
                            "answer": "搜索功能正常，结果显示正确，无匹配时有提示"
                        })
                        conn.request('POST', f'/api/tasks/{task_id}/answer', body=answer2_data,
                                    headers={'Content-Type': 'application/json'})
                        response = conn.getresponse()
                        self.assertEqual(response.status, 202)

                        # Wait for baseline
                        for _ in range(50):
                            conn.request('GET', f'/api/tasks/{task_id}')
                            response = conn.getresponse()
                            data = json.loads(response.read())
                            if data["status"] == "awaiting_confirmation":
                                break
                            time.sleep(0.1)

                        self.assertEqual(data["status"], "awaiting_confirmation")
                        self.assertIn("股票", data["baseline"]["refined_summary"])

                        # Verify workspace STILL empty before confirmation
                        self.assertEqual(len(list(workspace_path.iterdir())), 0)

                        # Step 5: POST confirm
                        conn.request('POST', f'/api/tasks/{task_id}/confirm',
                                    headers={'Content-Type': 'application/json'})
                        response = conn.getresponse()
                        self.assertEqual(response.status, 202)

                        # Wait for completion
                        for _ in range(100):
                            conn.request('GET', f'/api/tasks/{task_id}')
                            response = conn.getresponse()
                            data = json.loads(response.read())
                            if data["status"] in ("completed", "failed", "stopped"):
                                break
                            time.sleep(0.1)

                        # Verify final status
                        self.assertEqual(data["status"], "completed")
                        self.assertEqual(data["stop_reason"], "submitted")
                        self.assertGreater(data["patch"]["files"], 0)
                        self.assertGreater(data["patch"]["additions"], 0)

                        # Verify no paths in response
                        response_text = json.dumps(data)
                        self.assertNotIn("C:", response_text)
                        self.assertNotIn("D:", response_text)
                        self.assertNotIn("/home/", response_text)

                        # Verify workspace has files
                        self.assertGreater(len(list(workspace_path.iterdir())), 0)

                        # Try to confirm again (should fail)
                        conn.request('POST', f'/api/tasks/{task_id}/confirm',
                                    headers={'Content-Type': 'application/json'})
                        response = conn.getresponse()
                        self.assertEqual(response.status, 400)

                        conn.close()

                    finally:
                        server.shutdown()
                        server.server_close()
                        server_thread.join(timeout=2)
                        self.assertFalse(server_thread.is_alive())
                        self.assertTrue(all(record.process is None for record in server.tasks.tasks.values()))

                finally:
                    # Restore original TaskManager.__init__
                    TaskManager.__init__ = original_task_manager_init


if __name__ == "__main__":
    import unittest
    unittest.main()
