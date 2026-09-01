from __future__ import annotations

import argparse
import hashlib
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GUI_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = GUI_ROOT / "static"
ONTOLOGY_SOURCE = Path("configs/frozen/baseline-v3/requirement-ontology.json")
BASELINE_SOURCE = Path("configs/frozen/baseline-v3/baseline.json")
ANNOTATION_SOURCE = GUI_ROOT / "ontology_annotations.json"


class FrozenDataError(RuntimeError):
    """Raised when the read-only presentation data is malformed."""


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FrozenDataError(f"Unable to read {path.name}") from error
    if not isinstance(value, dict):
        raise FrozenDataError(f"Invalid object in {path.name}")
    return value


def load_ontology(project_root: Path = PROJECT_ROOT) -> dict[str, object]:
    ontology_path = project_root / ONTOLOGY_SOURCE
    baseline_path = project_root / BASELINE_SOURCE
    try:
        ontology_bytes = ontology_path.read_bytes()
    except OSError as error:
        raise FrozenDataError("Unable to read requirement-ontology.json") from error
    try:
        ontology = json.loads(ontology_bytes)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise FrozenDataError("Invalid requirement-ontology.json") from error
    baseline = _read_json(baseline_path)
    expected = baseline.get("requirement_ontology_sha256")
    actual = hashlib.sha256(ontology_bytes).hexdigest()
    verified = isinstance(expected, str) and actual == expected

    response: dict[str, object] = {
        "baseline": baseline.get("name", "baseline-v3"),
        "version": ontology.get("version") if isinstance(ontology, dict) else None,
        "source": ONTOLOGY_SOURCE.as_posix(),
        "expected_sha256": expected,
        "actual_sha256": actual,
        "verified": verified,
        "category_count": 0,
        "slot_count": 0,
        "tree": None,
        "annotations": None,
    }
    if not verified:
        response["integrity_error"] = "Frozen ontology integrity verification failed. Tree content is unavailable."
        return response

    source_tree = ontology.get("ontology") if isinstance(ontology, dict) else None
    version = ontology.get("version") if isinstance(ontology, dict) else None
    if not isinstance(source_tree, dict) or not isinstance(version, str):
        raise FrozenDataError("Frozen ontology structure is invalid")

    annotations = _read_json(ANNOTATION_SOURCE)
    category_notes = annotations.get("categories")
    slot_notes = annotations.get("slots")
    if not isinstance(category_notes, dict) or not isinstance(slot_notes, dict):
        raise FrozenDataError("Ontology annotations are invalid")

    normalized = []
    expected_slots: set[str] = set()
    for category_id, slots in source_tree.items():
        if not isinstance(category_id, str) or not isinstance(slots, list) or not all(isinstance(slot, str) for slot in slots):
            raise FrozenDataError("Frozen ontology structure is invalid")
        if category_id not in category_notes:
            raise FrozenDataError("Ontology category annotations do not match the frozen tree")
        children = []
        for slot_id in slots:
            expected_slots.add(slot_id)
            if slot_id not in slot_notes:
                raise FrozenDataError("Ontology slot annotations do not match the frozen tree")
            children.append({"id": slot_id, "type": "slot"})
        normalized.append({"id": category_id, "type": "category", "children": children})
    if set(category_notes) != set(source_tree) or set(slot_notes) != expected_slots:
        raise FrozenDataError("Ontology annotations do not correspond exactly to the frozen tree")

    response.update({
        "category_count": len(normalized),
        "slot_count": len(expected_slots),
        "tree": {
            "id": "coding-requirement-ontology",
            "type": "root",
            "children": normalized,
        },
        "annotations": annotations,
    })
    return response


class DemoServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], ontology: dict[str, object]):
        super().__init__(address, DemoHandler)
        self.ontology = ontology


class DemoHandler(BaseHTTPRequestHandler):
    server_version = "ReqCodingAgentDemo"
    sys_version = ""
    static_files = {
        "/": ("index.html", "text/html; charset=utf-8"),
        "/settings": ("index.html", "text/html; charset=utf-8"),
        "/settings/general": ("index.html", "text/html; charset=utf-8"),
        "/settings/agent": ("index.html", "text/html; charset=utf-8"),
        "/settings/runtime": ("index.html", "text/html; charset=utf-8"),
        "/settings/ontology": ("index.html", "text/html; charset=utf-8"),
        "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
        "/static/styles.css": ("styles.css", "text/css; charset=utf-8"),
    }

    def log_message(self, format: str, *args: object) -> None:
        return

    def end_headers(self) -> None:
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        if parsed.query or ".." in Path(path).parts:
            self._not_found()
            return
        if path == "/api/health":
            verified = bool(self.server.ontology.get("verified"))
            self._json({"status": "ok" if verified else "integrity_failure", "read_only": True, "ontology_verified": verified}, HTTPStatus.OK if verified else HTTPStatus.CONFLICT)
        elif path == "/api/ontology":
            status = HTTPStatus.OK if self.server.ontology.get("verified") else HTTPStatus.CONFLICT
            self._json(self.server.ontology, status)
        elif path in self.static_files:
            filename, content_type = self.static_files[path]
            self._bytes((STATIC_ROOT / filename).read_bytes(), content_type)
        else:
            self._not_found()

    def do_HEAD(self) -> None:
        self._method_not_allowed()

    do_POST = do_HEAD
    do_PUT = do_HEAD
    do_PATCH = do_HEAD
    do_DELETE = do_HEAD

    def _json(self, payload: object, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._bytes(body, "application/json; charset=utf-8", status)

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
    return DemoServer((host, port), load_ontology(project_root))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the ReqCodingAgent read-only demo GUI")
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1",))
    parser.add_argument("--port", default=8765, type=int)
    args = parser.parse_args(argv)
    try:
        server = create_server(args.host, args.port)
    except FrozenDataError as error:
        print(f"Cannot start: {error}")
        return 1
    print(f"ReqCodingAgent demo available at http://{args.host}:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
