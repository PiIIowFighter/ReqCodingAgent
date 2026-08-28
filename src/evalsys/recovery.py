from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .domain import REPLAY_STATUSES
from .persistence import atomic_json
from .schema import validate_json

TERMINAL_STATUSES = REPLAY_STATUSES


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def compute_input_fingerprint(value: object) -> str:
    return fingerprint(value)


def artifact_hashes_valid(root: Path, expected: dict[str, str]) -> bool:
    return bool(expected) and all((root / name).is_file() and sha256_file(root / name) == digest for name, digest in expected.items())


def _tree_manifest(directory: Path, trees: list[str]) -> dict:
    files = []
    for tree in sorted(trees):
        root = directory / tree
        if not root.is_dir():
            raise FileNotFoundError(root)
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            files.append({"path": path.relative_to(directory).as_posix(), "sha256": sha256_file(path), "bytes": path.stat().st_size})
    return {"schema_version": "1.0", "files": files}


def _tree_manifest_valid(directory: Path) -> bool:
    path = directory / "harness-manifest.json"
    if not path.is_file():
        return True
    manifest = json.loads(path.read_text(encoding="utf-8"))
    trees = sorted({entry["path"].split("/", 1)[0] for entry in manifest["files"]})
    return _tree_manifest(directory, trees) == manifest


def write_completed_run(directory: Path, result: dict, input_fingerprint: str, key_artifacts: list[str], *, artifact_trees: list[str] | None = None) -> dict:
    validate_json(result, "replay-result")
    directory.mkdir(parents=True, exist_ok=True)
    atomic_json(directory / "result.json", result)
    names = list(key_artifacts) + ["result.json"]
    if artifact_trees:
        atomic_json(directory / "harness-manifest.json", _tree_manifest(directory, artifact_trees))
        names.append("harness-manifest.json")
    artifacts = {name: sha256_file(directory / name) for name in sorted(set(names))}
    marker = {"schema_version": "1.0", "input_fingerprint": input_fingerprint, "result": "result.json", "artifacts": artifacts}
    validate_json(marker, "completion")
    atomic_json(directory / "COMPLETE", marker)
    return marker


def load_reusable_run(directory: Path, expected_fingerprint: str) -> dict | None:
    try:
        if any(path.name.endswith(".tmp") for path in directory.iterdir()):
            return None
        marker = json.loads((directory / "COMPLETE").read_text(encoding="utf-8"))
        validate_json(marker, "completion")
        if marker["input_fingerprint"] != expected_fingerprint or not artifact_hashes_valid(directory, marker["artifacts"]) or not _tree_manifest_valid(directory):
            return None
        result = json.loads((directory / marker["result"]).read_text(encoding="utf-8"))
        validate_json(result, "replay-result")
        return result if result["status"] in TERMINAL_STATUSES else None
    except Exception:
        return None
