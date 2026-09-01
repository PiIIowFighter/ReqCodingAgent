from __future__ import annotations

import argparse
import hashlib
import json
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GUI_ROOT = Path(__file__).resolve().parent
FROZEN_RELATIVE = Path("configs/frozen/baseline-v3")


class FrozenDataError(RuntimeError):
    """Raised when frozen input cannot be verified safely."""


def load_frozen_data(project_root: Path = PROJECT_ROOT) -> dict[str, object]:
    frozen_root = project_root / FROZEN_RELATIVE
    ontology_path = frozen_root / "requirement-ontology.json"
    baseline_path = frozen_root / "baseline.json"
    try:
        ontology_bytes = ontology_path.read_bytes()
        ontology = json.loads(ontology_bytes)
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FrozenDataError("Frozen ontology inputs are unavailable or invalid") from error

    actual = hashlib.sha256(ontology_bytes).hexdigest()
    expected = baseline.get("requirement_ontology_sha256")
    if not isinstance(expected, str) or actual != expected:
        raise FrozenDataError("Frozen ontology SHA-256 verification failed")

    source_tree = ontology.get("ontology")
    version = ontology.get("version")
    if not isinstance(source_tree, dict) or not isinstance(version, str):
        raise FrozenDataError("Frozen ontology structure is invalid")
    tree = []
    for label, keys in source_tree.items():
        if not isinstance(label, str) or not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
            raise FrozenDataError("Frozen ontology structure is invalid")
        tree.append({"label": label, "children": [{"key": key} for key in keys]})
    return {"version": version, "sha256": actual, "tree": tree}


def load_annotations() -> dict[str, object]:
    try:
        payload = json.loads((GUI_ROOT / "annotation.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FrozenDataError("Annotation data is unavailable or invalid") from error
    if not isinstance(payload.get("disclaimer"), str) or not isinstance(payload.get("annotations"), dict):
        raise FrozenDataError("Annotation data is invalid")
    return payload


class DemoServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], ontology: dict[str, object], annotations: dict[str, object]):
        super().__init__(address, DemoHandler)
        self.ontology = ontology
        self.annotations = annotations


class DemoHandler(BaseHTTPRequestHandler):
    server_version = "LocalViewer"
    sys_version = ""
    static_files = {
        "/": ("index.html", "text/html; charset=utf-8"),
        "/settings": ("index.html", "text/html; charset=utf-8"),
        "/app.js": ("app.js", "text/javascript; charset=utf-8"),
        "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    }

    def log_message(self, format: str, *args: object) -> None:
        return

    def end_headers(self) -> None:
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        if parsed.query:
            self._not_found()
            return
        if path == "/api/health":
            self._json({"status": "ok", "data": "verified", "read_only": True})
        elif path == "/api/ontology":
            self._json(self.server.ontology)
        elif path == "/api/annotations":
            self._json(self.server.annotations)
        elif path in self.static_files:
            filename, content_type = self.static_files[path]
            self._bytes((GUI_ROOT / filename).read_bytes(), content_type)
        else:
            self._not_found()

    def do_HEAD(self) -> None:
        self._method_not_allowed()

    def do_POST(self) -> None:
        self._method_not_allowed()

    def do_PUT(self) -> None:
        self._method_not_allowed()

    def do_PATCH(self) -> None:
        self._method_not_allowed()

    def do_DELETE(self) -> None:
        self._method_not_allowed()

    def _json(self, payload: object, status: int = HTTPStatus.OK) -> None:
        self._bytes(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), "application/json; charset=utf-8", status)

    def _bytes(self, body: bytes, content_type: str, status: int = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _not_found(self) -> None:
        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _method_not_allowed(self) -> None:
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET")
        body = b'{"error":"method not allowed"}'
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def create_server(host: str = "127.0.0.1", port: int = 8765, project_root: Path = PROJECT_ROOT) -> DemoServer:
    ontology = load_frozen_data(project_root)
    annotations = load_annotations()
    ontology_keys = {child["key"] for group in ontology["tree"] for child in group["children"]}
    if set(annotations["annotations"]) != ontology_keys:
        raise FrozenDataError("Annotations do not correspond exactly to the frozen ontology")
    return DemoServer((host, port), ontology, annotations)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the read-only local requirement explorer")
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1",), help="loopback host")
    parser.add_argument("--port", default=8765, type=int, help="local TCP port")
    args = parser.parse_args(argv)
    try:
        server = create_server(args.host, args.port)
    except FrozenDataError as error:
        print(f"Cannot start: {error}", file=sys.stderr)
        return 1
    print(f"Requirement Explorer available at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
