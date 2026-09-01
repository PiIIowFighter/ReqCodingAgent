from __future__ import annotations

import hashlib
import http.client
import importlib
import json
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
            "127.0.0.1", 0, workspace=cls.workspace, config=cls.config, artifact_root=base / "runs"
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

    def test_health_runtime_and_ontology(self):
        status, _, body = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"status": "ok", "read_only": False, "ontology_verified": True})
        runtime = json.loads(self.request("GET", "/api/runtime")[2])
        self.assertEqual(runtime["workspace"], "workspace")
        self.assertEqual(runtime["mode"], "scripted")
        self.assertNotIn(str(self.workspace), json.dumps(runtime))
        source = ROOT / "configs/frozen/baseline-v3/requirement-ontology.json"
        baseline = json.loads((ROOT / "configs/frozen/baseline-v3/baseline.json").read_text(encoding="utf-8"))
        ontology = json.loads(self.request("GET", "/api/ontology")[2])
        self.assertEqual(ontology["expected_sha256"], baseline["requirement_ontology_sha256"])
        self.assertEqual(ontology["actual_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual((ontology["category_count"], ontology["slot_count"]), (4, 11))

    def test_scripted_agent_task_events_and_patch_download(self):
        task = "Update hello.py so VALUE becomes 2 when imported, preserve all other behavior, and verify the focused change with a test."
        status, _, body = self.request("POST", "/api/tasks", {"task": task})
        self.assertEqual(status, 202)
        task_id = json.loads(body)["id"]
        result = self.wait_for_task(task_id)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["stop_reason"], "submitted")
        self.assertEqual(result["patch"]["files"], 1)
        events = json.loads(self.request("GET", f"/api/tasks/{task_id}/events?after=0")[2])
        self.assertTrue(events["complete"])
        self.assertEqual({event["kind"] for event in events["events"]}, {"model_response", "tool_result"})
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

    def test_task_input_security_and_methods(self):
        self.assertEqual(self.request("POST", "/api/tasks", {"task": "", "workspace": "x"})[0], 400)
        self.assertEqual(self.request("POST", "/api/tasks", {"task": "x"}, {"Origin": "http://evil.invalid"})[0], 403)
        self.assertEqual(self.request("POST", "/api/tasks", {"task": "x"}, {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("PUT", "/api/tasks")[0], 405)

    def test_static_allowlist_shell_and_accessibility(self):
        for path in ("/../configs/frozen/baseline-v3/baseline.json", "/%2e%2e/configs/frozen/baseline-v3/baseline.json", "/server.py"):
            self.assertEqual(self.request("GET", path)[0], 404)
        for path in ("/", "/settings/ontology", "/settings/general", "/static/app.js", "/static/styles.css", "/static/task.css"):
            self.assertEqual(self.request("GET", path)[0], 200)
        html = self.request("GET", "/settings/ontology")[2].decode("utf-8")
        for text in ("ReqCodingAgent", "New task", "Back to workspace", "Coding Requirement Ontology", "Download patch"):
            self.assertIn(text, html)
        script = self.request("GET", "/static/app.js")[2].decode("utf-8")
        for hook in ("localStorage", "history.pushState", "ArrowDown", "aria-expanded", "/api/tasks", "next_offset"):
            self.assertIn(hook, script)
        self.assertIn('role="tree"', html)
        self.assertNotIn("cdn.", html.lower())

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


if __name__ == "__main__":
    unittest.main()
