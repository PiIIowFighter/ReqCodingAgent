from __future__ import annotations

import hashlib
import http.client
import importlib
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class DemoGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module("demo_gui.server")
        cls.httpd = cls.module.create_server("127.0.0.1", 0)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)

    def request(self, method: str, path: str):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(method, path)
        response = connection.getresponse()
        body = response.read()
        headers = dict(response.getheaders())
        connection.close()
        return response.status, headers, body

    def test_health_is_read_only_and_verified(self):
        status, _, body = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"status": "ok", "read_only": True, "ontology_verified": True})
        self.assertEqual(self.request("POST", "/api/health")[0], 405)

    def test_ontology_api_matches_frozen_sha_and_shape(self):
        source = ROOT / "configs/frozen/baseline-v3/requirement-ontology.json"
        baseline = json.loads((ROOT / "configs/frozen/baseline-v3/baseline.json").read_text(encoding="utf-8"))
        status, _, body = self.request("GET", "/api/ontology")
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["baseline"], "baseline-v3")
        self.assertEqual(payload["version"], "coding-requirement-ontology-v1")
        self.assertEqual(payload["source"], "configs/frozen/baseline-v3/requirement-ontology.json")
        self.assertEqual(payload["expected_sha256"], baseline["requirement_ontology_sha256"])
        self.assertEqual(payload["actual_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertTrue(payload["verified"])
        self.assertEqual(payload["category_count"], 4)
        self.assertEqual(payload["slot_count"], 11)
        self.assertEqual(payload["tree"]["type"], "root")

    def test_annotations_correspond_exactly_to_frozen_nodes(self):
        payload = json.loads(self.request("GET", "/api/ontology")[2])
        categories = {node["id"] for node in payload["tree"]["children"]}
        slots = {slot["id"] for category in payload["tree"]["children"] for slot in category["children"]}
        self.assertEqual(set(payload["annotations"]["categories"]), categories)
        self.assertEqual(set(payload["annotations"]["slots"]), slots)
        self.assertEqual(payload["annotations"]["statuses"], ["explicit", "inferred", "defaulted", "unresolved"])
        for annotation in payload["annotations"]["slots"].values():
            self.assertEqual(set(annotation), {"name_zh", "definition", "importance", "evidence", "example"})

    def test_api_does_not_leak_sensitive_or_absolute_paths(self):
        for route in ("/api/health", "/api/ontology"):
            body = self.request("GET", route)[2].decode("utf-8").lower()
            for forbidden in ("anthropic_auth_token", "api_key", "secret", "d:/", "d:\\", "c:/users", "benchmark case"):
                self.assertNotIn(forbidden, body)

    def test_static_allowlist_and_routes(self):
        for path in ("/../configs/frozen/baseline-v3/baseline.json", "/%2e%2e/configs/frozen/baseline-v3/baseline.json", "/server.py", "/ontology_annotations.json"):
            self.assertEqual(self.request("GET", path)[0], 404, path)
        for path in ("/", "/settings/ontology", "/settings/general", "/static/app.js", "/static/styles.css"):
            self.assertEqual(self.request("GET", path)[0], 200, path)

    def test_shell_contains_required_workspace_and_accessibility_hooks(self):
        html = self.request("GET", "/settings/ontology")[2].decode("utf-8")
        for text in ("ReqCodingAgent", "New task", "Tasks", "Evaluation", "Settings", "What would you like to build?", "Coding Requirement Ontology", "Expand all", "Collapse all"):
            self.assertIn(text, html)
        script = self.request("GET", "/static/app.js")[2].decode("utf-8")
        for hook in ("localStorage", "history.pushState", "ArrowDown", "ArrowRight", "aria-expanded", "phase"):
            if hook != "phase":
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
            self.assertIn("integrity_error", payload)


if __name__ == "__main__":
    unittest.main()
