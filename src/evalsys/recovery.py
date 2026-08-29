from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath

from .domain import REPLAY_STATUSES
from .persistence import atomic_json, file_lock
from .errors import EvalError
from .schema import strict_json_loads, validate_json

TERMINAL_STATUSES = REPLAY_STATUSES
REQUIRED_REPLAY_ARTIFACTS = frozenset({
    "stdout.log", "stderr.log", "events.jsonl", "dataset.json",
    "prediction.jsonl", "result.json", "harness-manifest.json",
})
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:")


def _is_reparse(path: Path) -> bool:
    return bool(getattr(path.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def safe_relative_path(root: Path, name: str, *, expected_type: str | None = "file") -> Path:
    if not isinstance(name, str) or not name or "\\" in name or "//" in name or _WINDOWS_ABSOLUTE.match(name):
        raise ValueError(f"unsafe noncanonical relative path: {name!r}")
    relative = PurePosixPath(name)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts) or relative.as_posix() != name:
        raise ValueError(f"unsafe noncanonical relative path: {name!r}")
    root = root.resolve()
    candidate = root.joinpath(*relative.parts)
    try:
        candidate.resolve().relative_to(root)
    except (OSError, ValueError) as exc:
        raise ValueError(f"relative path escapes root: {name}") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if not os.path.lexists(current):
            continue
        if current.is_symlink() or _is_reparse(current):
            raise ValueError(f"relative path uses symlink or reparse point: {name}")
    if expected_type == "file" and not candidate.is_file():
        raise ValueError(f"relative path is not a file: {name}")
    if expected_type == "directory" and not candidate.is_dir():
        raise ValueError(f"relative path is not a directory: {name}")
    return candidate


def _read_stable(path: Path) -> bytes:
    before = path.stat()
    with path.open("rb") as stream:
        data = stream.read()
        inside = os.fstat(stream.fileno())
    after = path.stat()
    identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
    if identity(before) != identity(inside) or identity(inside) != identity(after):
        raise ValueError(f"file changed while being read: {path}")
    return data


def sha256_file(path: Path) -> str:
    return hashlib.sha256(_read_stable(path)).hexdigest()


def fingerprint(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def compute_input_fingerprint(value: object) -> str:
    return fingerprint(value)


def artifact_hashes_valid(root: Path, expected: dict[str, str]) -> bool:
    if not expected:
        return False
    try:
        return all(
            re.fullmatch(r"[0-9a-f]{64}", digest) is not None
            and sha256_file(safe_relative_path(root, name)) == digest
            for name, digest in expected.items()
        )
    except (OSError, TypeError, ValueError):
        return False


def _tree_manifest(directory: Path, trees: list[str]) -> dict:
    files = []
    for tree in sorted(trees):
        root = safe_relative_path(directory, tree, expected_type="directory")
        for walk_root, directories, names in os.walk(root, topdown=True, followlinks=False):
            directories.sort()
            current = Path(walk_root)
            for name in directories:
                safe_relative_path(directory, (current / name).relative_to(directory).as_posix(), expected_type="directory")
            for name in sorted(names):
                path = safe_relative_path(directory, (current / name).relative_to(directory).as_posix())
                data = _read_stable(path)
                files.append({"path": path.relative_to(directory).as_posix(), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
    return {"schema_version": "1.0", "roots": sorted(trees), "files": files}


def _tree_manifest_valid(directory: Path) -> bool:
    path = safe_relative_path(directory, "harness-manifest.json")
    manifest = strict_json_loads(_read_stable(path).decode("utf-8"))
    if not isinstance(manifest, dict) or set(manifest) != {"schema_version", "roots", "files"} or manifest.get("roots") != ["harness"]:
        return False
    files = manifest.get("files")
    if not isinstance(files, list) or any(not isinstance(item, dict) or not isinstance(item.get("path"), str) for item in files):
        return False
    actual = _tree_manifest(directory, ["harness"])
    expected_files = sorted(files, key=lambda item: item["path"])
    actual_files = sorted(actual["files"], key=lambda item: item["path"])
    return manifest.get("schema_version") == actual["schema_version"] and expected_files == actual_files


def write_completed_run(directory: Path, result: dict, input_fingerprint: str, key_artifacts: list[str], *, artifact_trees: list[str] | None = None) -> dict:
    validate_json(result, "replay-result")
    required_inputs = REQUIRED_REPLAY_ARTIFACTS - {"result.json", "harness-manifest.json"}
    if set(key_artifacts) != required_inputs or len(key_artifacts) != len(required_inputs):
        raise ValueError("completion requires exactly the replay artifact inputs")
    if artifact_trees != ["harness"]:
        raise ValueError("completion requires exactly the harness tree root")
    directory.mkdir(parents=True, exist_ok=True)
    with file_lock(directory / ".checkpoint.lock"):
        for name in key_artifacts:
            safe_relative_path(directory, name)
        atomic_json(directory / "result.json", result)
        atomic_json(directory / "harness-manifest.json", _tree_manifest(directory, artifact_trees))
        artifacts = {name: sha256_file(safe_relative_path(directory, name)) for name in sorted(REQUIRED_REPLAY_ARTIFACTS)}
        marker = {"schema_version": "1.0", "input_fingerprint": input_fingerprint, "result": "result.json", "artifacts": artifacts}
        validate_json(marker, "completion")
        atomic_json(directory / "COMPLETE", marker)
    return marker


def load_reusable_run(directory: Path, expected_fingerprint: str) -> dict | None:
    try:
        with file_lock(directory / ".checkpoint.lock"):
            if any(path.name.endswith(".tmp") for path in directory.iterdir()):
                return None
            marker_path = safe_relative_path(directory, "COMPLETE")
            marker = strict_json_loads(_read_stable(marker_path).decode("utf-8"))
            validate_json(marker, "completion")
            if set(marker["artifacts"]) != REQUIRED_REPLAY_ARTIFACTS:
                return None
            if marker["input_fingerprint"] != expected_fingerprint:
                return None
            snapshots: dict[str, bytes] = {}
            for name, digest in marker["artifacts"].items():
                path = safe_relative_path(directory, name)
                data = _read_stable(path)
                if hashlib.sha256(data).hexdigest() != digest:
                    return None
                snapshots[name] = data
            if not _tree_manifest_valid(directory):
                return None
            result = strict_json_loads(snapshots["result.json"].decode("utf-8"))
            validate_json(result, "replay-result")
            return result if result["status"] in TERMINAL_STATUSES else None
    except EvalError as exc:
        if exc.category == "infra_failed":
            raise
        return None
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError, ValueError, KeyError, TypeError):
        return None
    except (PermissionError, OSError) as exc:
        raise EvalError(f"Cannot access replay checkpoint: {exc}", category="infra_failed", hint="Fix artifact storage permissions; the checkpoint was not modified") from exc
