from __future__ import annotations

import hashlib
import http.client
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class DemoGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module("demo_gui.server")
        cls.temp = tempfile.TemporaryDirectory()
        base = Path(cls.temp.name)
        cls.workspace = base / "workspace"
        cls.workspace.mkdir()
        (cls.workspace / "hello.py").write_text("VALUE = 1\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=cls.workspace, check=True)
        subprocess.run(["git", "config", "user.email", "demo@example.invalid"], cwd=cls.workspace, check=True)
        subprocess.run(["git", "config", "user.name", "Demo"], cwd=cls.workspace, check=True)
        subprocess.run(["git", "add", "hello.py"], cwd=cls.workspace, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=cls.workspace, check=True)
        base_config = json.loads((ROOT / "configs/agent/offline-scripted.json").read_text(encoding="utf-8"))
        base_config["script"] = [
            {
                "text": "Apply the requested focused change.",
                "tool_calls": [{"call_id": "call-1", "name": "apply_patch", "arguments": {
                    "patch": "*** Begin Patch\n*** Update File: hello.py\n@@\n-VALUE = 1\n+VALUE = 2\n*** End Patch"
                }}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "finish_reason": "tool_calls", "provider_request_id": "demo-1",
            },
            {
                "text": "Report the completed change.",
                "tool_calls": [{"call_id": "call-2", "name": "submit", "arguments": {
                    "summary": "Updated the requested value.", "tests": [], "limitations": "Deterministic GUI smoke."
                }}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "finish_reason": "tool_calls", "provider_request_id": "demo-2",
            },
        ]
        cls.config = base / "scripted.json"
        cls.config.write_text(json.dumps(base_config), encoding="utf-8")
        cls.httpd = cls.module.create_server(
            "127.0.0.1", 0, workspace=cls.workspace, config=cls.config, artifact_root=base / "runs", in_place=False
        )
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)
        cls.temp.cleanup()

    def request(self, method: str, path: str, payload=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request_headers = dict(headers or {})
        if body is not None:
            request_headers.setdefault("Content-Type", "application/json")
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        content = response.read()
        response_headers = dict(response.getheaders())
        connection.close()
        return response.status, response_headers, content

    def wait_for_task(self, task_id: str):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status, _, body = self.request("GET", f"/api/tasks/{task_id}")
            self.assertEqual(status, 200)
            payload = json.loads(body)
            if payload["status"] in {"completed", "failed"}:
                return payload
            time.sleep(0.05)
        self.fail("task did not finish")

    def test_direct_script_entrypoint_can_import_interview_package(self):
        server_path = ROOT / "demo_gui/server.py"
        command = (
            "import runpy; "
            f"runpy.run_path({str(server_path)!r}, run_name='direct_entrypoint_test'); "
            "import demo_gui.interview"
        )
        completed = subprocess.run(
            [sys.executable, "-c", command],
            cwd=self.temp.name,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_health_runtime_and_ontology(self):
        status, _, body = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"status": "ok", "read_only": False, "ontology_verified": True})
        runtime = json.loads(self.request("GET", "/api/runtime")[2])
        self.assertEqual(runtime["workspace_path"], str(self.workspace.resolve()))
        self.assertTrue(runtime["ready"])
        source = ROOT / "configs/frozen/baseline-v3/requirement-ontology.json"
        baseline = json.loads((ROOT / "configs/frozen/baseline-v3/baseline.json").read_text(encoding="utf-8"))
        ontology = json.loads(self.request("GET", "/api/ontology")[2])
        self.assertEqual(ontology["expected_sha256"], baseline["requirement_ontology_sha256"])
        self.assertEqual(ontology["actual_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual((ontology["category_count"], ontology["slot_count"]), (4, 11))

    def test_live_environment_preflight_checks_names_without_values(self):
        config = self.module.PROJECT_ROOT / "configs/agent/demo-openai.json"
        original = dict(os.environ)
        try:
            os.environ.pop("OPENAI_BASE_URL", None)
            os.environ.pop("OPENAI_API_KEY", None)
            with self.assertRaisesRegex(RuntimeError, "OPENAI_BASE_URL, OPENAI_API_KEY"):
                self.module._validate_live_env(config)
        finally:
            os.environ.clear()
            os.environ.update(original)

    def test_stop_reason_status_semantics(self):
        self.assertEqual(self.module.status_for_stop_reason("submitted"), "completed")
        for reason in ("step_budget", "repeated_action", "tool_budget", "wall_clock_timeout"):
            self.assertEqual(self.module.status_for_stop_reason(reason), "stopped")
        self.assertEqual(self.module.status_for_stop_reason("unrecoverable_model_error"), "failed")

    def test_workspace_selection_and_task_without_workspace(self):
        temp = tempfile.TemporaryDirectory()
        try:
            base = Path(temp.name)
            empty = base / "empty"
            empty.mkdir()
            httpd = self.module.create_server("127.0.0.1", 0, workspace=None, config=self.config, artifact_root=base / "runs", in_place=False)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            port = httpd.server_address[1]
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", "/api/runtime")
            runtime = json.loads(connection.getresponse().read())
            connection.close()
            self.assertIsNone(runtime["workspace_path"])
            self.assertFalse(runtime["ready"])
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("POST", "/api/tasks", json.dumps({"task": "x"}).encode("utf-8"), {"Content-Type": "application/json"})
            self.assertEqual(connection.getresponse().status, 409)
            connection.close()
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("POST", "/api/workspace", json.dumps({"path": str(self.workspace)}).encode("utf-8"), {"Content-Type": "application/json"})
            runtime = json.loads(connection.getresponse().read())
            connection.close()
            self.assertEqual(runtime["workspace_path"], str(self.workspace.resolve()))
            self.assertTrue(runtime["ready"])
            (empty / "user.txt").write_text("preserve\n", encoding="utf-8")
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("POST", "/api/workspace", json.dumps({"path": str(empty)}).encode("utf-8"), {"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 409)
            response.read()
            connection.close()
            self.assertEqual((empty / "user.txt").read_text(encoding="utf-8"), "preserve\n")
            self.assertFalse((empty / ".git").exists())
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)
        finally:
            temp.cleanup()

    def test_workspace_selection_preflight_is_read_only_and_rejects_unsafe_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            empty = base / "empty"
            empty.mkdir()
            manager = self.module.TaskManager(empty, self.config, base / "runs", in_place=True)
            self.assertEqual(manager.workspace, empty.resolve())
            self.assertFalse((empty / ".git").exists())

            non_git = base / "non-git"
            non_git.mkdir()
            (non_git / "user.txt").write_text("preserve\n", encoding="utf-8")
            with self.assertRaisesRegex(self.module.WorkspaceError, "empty non-git"):
                manager.set_workspace(non_git)
            self.assertEqual((non_git / "user.txt").read_text(encoding="utf-8"), "preserve\n")
            self.assertFalse((non_git / ".git").exists())

            dirty = base / "dirty"
            dirty.mkdir()
            subprocess.run(["git", "-C", str(dirty), "init", "-q"], check=True)
            subprocess.run(["git", "-C", str(dirty), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(dirty), "config", "user.name", "Test"], check=True)
            (dirty / "tracked.txt").write_text("clean\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(dirty), "add", "."], check=True)
            subprocess.run(["git", "-C", str(dirty), "commit", "-qm", "initial"], check=True)
            (dirty / "tracked.txt").write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(self.module.WorkspaceError, "must be clean"):
                manager.set_workspace(dirty)
            (dirty / "subdir").mkdir()
            with self.assertRaisesRegex(self.module.WorkspaceError, "repository root"):
                manager.set_workspace(dirty / "subdir")

    def test_scripted_agent_task_events_and_patch_download(self):
        task = "Update hello.py so VALUE becomes 2 when imported, preserve all other behavior, and verify the focused change with a test."
        status, _, body = self.request("POST", "/api/tasks", {"task": task})
        self.assertEqual(status, 202)
        task_id = json.loads(body)["id"]
        result = self.wait_for_task(task_id)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["stop_reason"], "submitted")
        self.assertEqual(result["patch"]["files"], 1)
        self.assertEqual(result["route_decision"]["mode"], "fast")
        self.assertEqual(result["route_decision"]["source"], "interactive_router")
        events = json.loads(self.request("GET", f"/api/tasks/{task_id}/events?after=0")[2])
        self.assertTrue(events["complete"])
        # Include route_decision event that now appears due to adaptive routing
        self.assertTrue({"model_response", "tool_result"}.issubset({event["kind"] for event in events["events"]}))
        serialized = json.dumps(events).lower()
        for forbidden in ("input_tokens", "provider_request_id", str(self.workspace).lower(), "api_key"):
            self.assertNotIn(forbidden, serialized)
        patch = json.loads(self.request("GET", f"/api/tasks/{task_id}/patch")[2])
        self.assertIn("+VALUE = 2", patch["patch"])
        download_status, headers, download = self.request("GET", f"/api/tasks/{task_id}/patch/download")
        self.assertEqual(download_status, 200)
        self.assertEqual(headers["Content-Type"], "text/x-diff; charset=utf-8")
        self.assertIn(b"+VALUE = 2", download)
        self.assertEqual((self.workspace / "hello.py").read_text(encoding="utf-8"), "VALUE = 1\n")

    def test_stop_endpoint_releases_active_task(self):
        task_id = "f" * 32
        record = self.module.TaskRecord(task_id, "interview task", status="awaiting_user")
        with self.httpd.tasks.lock:
            self.httpd.tasks.tasks[task_id] = record
            self.httpd.tasks.active_id = task_id
        status, _, body = self.request("POST", f"/api/tasks/{task_id}/stop", {})
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(body), {"status": "stopped"})
        self.assertEqual(self.httpd.tasks.get(task_id).status, "stopped")
        self.assertFalse(json.loads(self.request("GET", "/api/runtime")[2])["active"])
        self.assertEqual(self.request("POST", f"/api/tasks/{task_id}/stop", {})[0], 400)

    def test_task_input_security_and_methods(self):
        self.assertEqual(self.request("POST", "/api/tasks", {"task": "", "workspace": "x"})[0], 400)
        self.assertEqual(self.request("POST", "/api/workspace", {"path": ""})[0], 400)
        self.assertEqual(self.request("POST", "/api/tasks", {"task": "x"}, {"Origin": "http://evil.invalid"})[0], 403)
        self.assertEqual(self.request("POST", "/api/tasks", {"task": "x"}, {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("PUT", "/api/tasks")[0], 405)

    def test_static_allowlist_shell_and_accessibility(self):
        for path in ("/../configs/frozen/baseline-v3/baseline.json", "/%2e%2e/configs/frozen/baseline-v3/baseline.json", "/server.py"):
            self.assertEqual(self.request("GET", path)[0], 404)
        for path in ("/", "/settings/ontology", "/static/app.js", "/static/styles.css", "/static/task.css"):
            self.assertEqual(self.request("GET", path)[0], 200)
        html = self.request("GET", "/settings/ontology")[2].decode("utf-8")
        for text in ("ReqCodingAgent", "新建任务", "返回工作区", "通用编码需求本体", "下载 Patch", "工作目录", "通用冻结层"):
            self.assertIn(text, html)
        for text in ("Evaluation", "General", "Model &amp; Runtime"):
            self.assertNotIn(text, html)
        script = self.request("GET", "/static/app.js")[2].decode("utf-8")
        for hook in ("localStorage", "history.pushState", "ArrowDown", "aria-expanded", "/api/tasks", "/api/workspace", "/stop", "next_offset"):
            self.assertIn(hook, script)
        self.assertIn('role="tree"', html)
        self.assertNotIn("cdn.", html.lower())

    def test_presentation_event_phases_and_safe_validation_summary(self):
        sanitize = self.module.TaskManager._sanitize_event
        command = sanitize(json.dumps({
            "kind": "tool_result", "phase": "main", "result": {
                "ok": True, "tool": "run_command", "data": {"command": "sh test_site.sh"}
            }
        }), 0)
        self.assertEqual(command["phase"], "verification")
        self.assertEqual(command["summary"], "Command succeeded: sh test_site.sh")

        patch = sanitize(json.dumps({
            "kind": "tool_result", "phase": "main", "result": {
                "ok": True, "tool": "apply_patch", "data": {"files": 2, "additions": 12, "deletions": 3}
            }
        }), 1)
        self.assertEqual(patch["phase"], "implementation")
        self.assertIn("2 files, +12, -3", patch["summary"])

        investigation = sanitize(json.dumps({
            "kind": "model_response", "phase": "main", "response": {
                "tool_calls": [{"name": "read_file"}], "text": "", "finish_reason": "tool_calls"
            }
        }), 2)
        self.assertEqual(investigation["phase"], "investigation")

    def test_security_headers_apply_to_success_and_errors(self):
        for method, path in (("GET", "/"), ("GET", "/missing"), ("POST", "/api/health")):
            _, headers, _ = self.request(method, path)
            self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(headers["Cache-Control"], "no-store")

    def test_integrity_failure_returns_no_tree(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            frozen = root / "configs/frozen/baseline-v3"
            frozen.mkdir(parents=True)
            (frozen / "requirement-ontology.json").write_text('{"version":"changed","ontology":{"Drift":["item"]}}', encoding="utf-8")
            (frozen / "baseline.json").write_text(json.dumps({"name": "baseline-v3", "requirement_ontology_sha256": "0" * 64}), encoding="utf-8")
            payload = self.module.load_ontology(root)
            self.assertFalse(payload["verified"])
            self.assertIsNone(payload["tree"])


class SubprocessCaptureTests(unittest.TestCase):
    def test_large_stdout_does_not_block_with_file_redirect(self):
        script = Path(tempfile.mkdtemp()) / "verbose_agent.py"
        try:
            script.write_text(
                "import json, sys\n"
                "sys.stdout.write('x' * 131072)\n"
                "sys.stdout.write('\\n')\n"
                "print(json.dumps({'run_id': 'capture-test', 'stop_reason': 'submitted', 'submitted': {'summary': 'ok'}, 'patch': {'files': 0, 'additions': 0, 'deletions': 0, 'bytes': 0}}))\n",
                encoding="utf-8",
            )
            capture_dir = tempfile.mkdtemp(prefix="reqagent-demo-capture-test-")
            stdout_path = Path(capture_dir) / "stdout.txt"
            stderr_path = Path(capture_dir) / "stderr.txt"
            try:
                with open(stdout_path, "w", encoding="utf-8", errors="replace") as stdout_fp, open(
                    stderr_path, "w", encoding="utf-8", errors="replace"
                ) as stderr_fp:
                    process = subprocess.Popen([sys.executable, str(script)], stdout=stdout_fp, stderr=stderr_fp)
                    deadline = time.monotonic() + 15
                    while process.poll() is None:
                        if time.monotonic() > deadline:
                            process.kill()
                            self.fail("subprocess blocked on large stdout")
                        time.sleep(0.05)
                    self.assertEqual(process.returncode, 0)
                stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
                payload = json.loads(stdout.strip().splitlines()[-1])
                self.assertEqual(payload["run_id"], "capture-test")
            finally:
                shutil.rmtree(capture_dir, ignore_errors=True)
        finally:
            shutil.rmtree(script.parent, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
