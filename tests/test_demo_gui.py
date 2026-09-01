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
        cls.server_module = importlib.import_module("demo_gui.server")
        cls.httpd = cls.server_module.create_server("127.0.0.1", 0)
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

    def test_health_is_get_only_and_reports_verified_frozen_data(self):
        status, _, body = self.request("GET", "/api/health")
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"status": "ok", "data": "verified", "read_only": True})
        self.assertEqual(self.request("POST", "/api/health")[0], 405)

    def test_ontology_has_exact_frozen_integrity_and_normalized_four_eleven_tree(self):
        frozen = ROOT / "configs/frozen/baseline-v3/requirement-ontology.json"
        baseline = json.loads((ROOT / "configs/frozen/baseline-v3/baseline.json").read_text(encoding="utf-8"))
        self.assertEqual(hashlib.sha256(frozen.read_bytes()).hexdigest(), baseline["requirement_ontology_sha256"])

        status, _, body = self.request("GET", "/api/ontology")
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["version"], "coding-requirement-ontology-v1")
        self.assertEqual(payload["sha256"], baseline["requirement_ontology_sha256"])
        self.assertEqual(len(payload["tree"]), 4)
        self.assertEqual(sum(len(group["children"]) for group in payload["tree"]), 11)
        self.assertEqual(
            [(group["label"], [child["key"] for child in group["children"]]) for group in payload["tree"]],
            [
                ("Change Intent", ["goal", "current_behavior_or_symptom", "expected_behavior"]),
                ("Code Scope", ["target_component", "relevant_symbol_or_api", "affected_consumers"]),
                ("Constraints", ["compatibility", "boundary_and_error_semantics", "excluded_scope"]),
                ("Validation", ["acceptance_criteria", "relevant_tests_or_checks"]),
            ],
        )

    def test_annotations_correspond_exactly_to_every_ontology_leaf(self):
        ontology = json.loads(self.request("GET", "/api/ontology")[2])
        annotations = json.loads(self.request("GET", "/api/annotations")[2])
        leaves = {child["key"] for group in ontology["tree"] for child in group["children"]}
        self.assertEqual(set(annotations["annotations"]), leaves)
        self.assertTrue(annotations["disclaimer"])
        for key, annotation in annotations["annotations"].items():
            self.assertEqual(annotation["key"], key)
            self.assertTrue(annotation["label"])
            self.assertTrue(annotation["description"])

    def test_public_responses_do_not_leak_sensitive_or_absolute_path_text(self):
        forbidden = [
            b"D-O1-fuzzy", b"django__", b"ANTHROPIC", b"api_key", b"total_tokens",
            b"D:/", b"D:\\", b"/workspace", b"427a957", b"resolved", b"score",
        ]
        for route in ("/", "/settings", "/api/health", "/api/ontology", "/api/annotations"):
            status, _, body = self.request("GET", route)
            self.assertEqual(status, 200, route)
            for needle in forbidden:
                self.assertNotIn(needle.lower(), body.lower(), (route, needle))

    def test_static_allowlist_refuses_traversal_unknown_files_and_query_bypasses(self):
        for path in (
            "/../configs/frozen/baseline-v3/baseline.json",
            "/%2e%2e/configs/frozen/baseline-v3/baseline.json",
            "/server.py",
            "/annotation.json?download=1",
            "/api/ontology/extra",
        ):
            self.assertEqual(self.request("GET", path)[0], 404, path)
        self.assertEqual(self.request("GET", "/app.js")[0], 200)
        self.assertEqual(self.request("GET", "/styles.css")[0], 200)

    def test_index_and_settings_routes_share_accessible_native_shell(self):
        for route in ("/", "/settings"):
            status, _, body = self.request("GET", route)
            text = body.decode("utf-8")
            self.assertEqual(status, 200)
            self.assertIn('data-route="ontology"', text)
            self.assertIn('data-route="settings"', text)
            self.assertIn('role="tree"', text)
            self.assertIn('aria-label="Search ontology"', text)
            self.assertIn('id="composer"', text)
            self.assertIn('type="button"', text)
            self.assertIn("<svg", text)
            self.assertNotIn("cdn.", text.lower())
        script = self.request("GET", "/app.js")[2].decode("utf-8")
        self.assertIn("localStorage", script)
        self.assertIn("history.pushState", script)
        self.assertIn("ArrowDown", script)
        self.assertIn("aria-expanded", script)

    def test_security_headers_apply_to_success_errors_and_method_refusals(self):
        required = {
            "Content-Security-Policy": "default-src 'self'",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Cache-Control": "no-store",
        }
        for method, route in (("GET", "/"), ("GET", "/missing"), ("POST", "/api/health")):
            _, headers, _ = self.request(method, route)
            for name, value in required.items():
                self.assertIn(value, headers.get(name, ""), (method, route, name))

    def test_hash_mismatch_fails_explicitly_without_serving_a_drift_tree(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            frozen = root / "configs/frozen/baseline-v3"
            frozen.mkdir(parents=True)
            (frozen / "requirement-ontology.json").write_text(
                '{"version":"changed","ontology":{"Drift":["item"]}}', encoding="utf-8"
            )
            (frozen / "baseline.json").write_text(
                json.dumps({"requirement_ontology_sha256": "0" * 64}), encoding="utf-8"
            )
            with self.assertRaisesRegex(self.server_module.FrozenDataError, "SHA-256 verification failed"):
                self.server_module.load_frozen_data(root)


if __name__ == "__main__":
    unittest.main()
