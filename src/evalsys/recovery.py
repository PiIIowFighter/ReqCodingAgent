from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from .schema import validate_json

TERMINAL_STATUSES = {"passed", "test_failed", "infra_failed", "timeout", "invalid"}


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


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_completed_run(directory: Path, result: dict, input_fingerprint: str, key_artifacts: list[str]) -> dict:
    validate_json(result, "replay-result")
    directory.mkdir(parents=True, exist_ok=True)
    _atomic_json(directory / "result.json", result)
    artifacts = {name: sha256_file(directory / name) for name in sorted(set(key_artifacts + ["result.json"]))}
    marker = {"schema_version": "1.0", "input_fingerprint": input_fingerprint, "result": "result.json", "artifacts": artifacts}
    validate_json(marker, "completion")
    _atomic_json(directory / "COMPLETE", marker)
    return marker


def load_reusable_run(directory: Path, expected_fingerprint: str) -> dict | None:
    try:
        if any(path.name.endswith(".tmp") for path in directory.iterdir()):
            return None
        marker = json.loads((directory / "COMPLETE").read_text(encoding="utf-8"))
        validate_json(marker, "completion")
        if marker["input_fingerprint"] != expected_fingerprint or not artifact_hashes_valid(directory, marker["artifacts"]):
            return None
        result = json.loads((directory / marker["result"]).read_text(encoding="utf-8"))
        validate_json(result, "replay-result")
        if result["status"] not in TERMINAL_STATUSES:
            return None
        return result
    except Exception:
        # Schema errors, missing files, and corruption are cache misses; callers rerun.
        return None
