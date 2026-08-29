from __future__ import annotations

import fnmatch
import re
from typing import Any

from ..model import ToolDefinition
from ..workspace import WorkspacePolicy
from .base import ToolEnvelope, object_schema
from .files import SKIP_NAMES


def definition() -> ToolDefinition:
    return ToolDefinition("search_text", "Search text in repository files. Call this to locate symbols and references.", object_schema({"query": {"type": "string"}, "path": {"type": "string"}, "glob": {"type": "string"}, "max_results": {"type": "integer", "minimum": 1}}, ["query"]))


def search_text(policy: WorkspacePolicy, args: dict[str, Any]) -> ToolEnvelope:
    root = policy.resolve(args.get("path", "."))
    try:
        pattern = re.compile(args["query"])
    except re.error as exc:
        raise ValueError(f"invalid search regex: {exc}") from exc
    glob = args.get("glob", "*")
    limit = args.get("max_results", 200)
    matches = []
    candidates = [root] if root.is_file() else root.rglob("*")
    for path in sorted(candidates):
        if not path.is_file():
            continue
        relative = path.relative_to(policy.root)
        if any(part in SKIP_NAMES for part in relative.parts) or not fnmatch.fnmatch(relative.as_posix(), glob):
            continue
        policy.resolve(relative.as_posix(), must_exist=True)
        raw = path.read_bytes()
        if b"\x00" in raw or len(raw) > 1024 * 1024:
            continue
        for number, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
            if pattern.search(line):
                matches.append({"path": relative.as_posix(), "line": number, "text": line[:500]})
                if len(matches) >= limit:
                    return ToolEnvelope(True, "search_text", {"matches": matches}, None, True, {})
    return ToolEnvelope(True, "search_text", {"matches": matches}, None, False, {})
