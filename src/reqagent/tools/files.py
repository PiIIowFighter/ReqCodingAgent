from __future__ import annotations

from pathlib import Path
from typing import Any

from ..model import ToolDefinition
from ..workspace import WorkspacePolicy
from .base import ToolEnvelope, object_schema


SKIP_NAMES = {".git", ".pytest_cache", "__pycache__", ".mypy_cache", ".ruff_cache"}


def list_definition() -> ToolDefinition:
    return ToolDefinition("list_files", "List repository files. Call this to locate relevant code before editing.", object_schema({"path": {"type": "string"}, "depth": {"type": "integer", "minimum": 0}, "max_entries": {"type": "integer", "minimum": 1}}, ["path"]))


def read_definition() -> ToolDefinition:
    return ToolDefinition("read_file", "Read a repository text file with line numbers. Call this before changing a file.", object_schema({"path": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1}, "end_line": {"type": "integer", "minimum": 1}}, ["path"]))


def _tool_path(raw: object) -> str:
    if not isinstance(raw, str):
        raise ValueError("path must be a string")
    path = raw.strip()
    return path or "."


def list_files(policy: WorkspacePolicy, args: dict[str, Any]) -> ToolEnvelope:
    root = policy.resolve(_tool_path(args["path"]))
    if not root.exists():
        return ToolEnvelope(True, "list_files", {"entries": []}, None, False, {})
    depth = args.get("depth", 3)
    limit = args.get("max_entries", 200)
    base_parts = len(root.relative_to(policy.root).parts)
    entries = []
    candidates = [root] if root.is_file() else root.rglob("*")
    for candidate in candidates:
        relative = candidate.relative_to(policy.root)
        if any(part in SKIP_NAMES for part in relative.parts):
            continue
        if len(relative.parts) - base_parts > depth:
            continue
        policy.resolve(relative.as_posix(), must_exist=True)
        entries.append({"path": relative.as_posix(), "type": "directory" if candidate.is_dir() else "file"})
    entries.sort(key=lambda item: item["path"])
    return ToolEnvelope(True, "list_files", {"entries": entries[:limit]}, None, len(entries) > limit, {})


def read_file(policy: WorkspacePolicy, args: dict[str, Any]) -> ToolEnvelope:
    path = policy.resolve(_tool_path(args["path"]), must_exist=True)
    if not path.is_file():
        raise ValueError("path is not a file")
    raw = path.read_bytes()
    if len(raw) > 1024 * 1024 or b"\x00" in raw:
        raise ValueError("binary or oversized file")
    text = raw.decode("utf-8")
    lines = text.splitlines()
    start = args.get("start_line", 1)
    end = min(args.get("end_line", start + 399), len(lines))
    if end < start:
        raise ValueError("end_line must be at least start_line")
    selected = [f"{index}\t{lines[index - 1]}" for index in range(start, end + 1)]
    output = "\n".join(selected)
    encoded = output.encode("utf-8")
    truncated = end < len(lines) or len(encoded) > 65536
    if len(encoded) > 65536:
        output = encoded[:65536].decode("utf-8", errors="ignore")
    return ToolEnvelope(True, "read_file", {"path": path.relative_to(policy.root).as_posix(), "content": output, "start_line": start, "end_line": end}, None, truncated, {})
