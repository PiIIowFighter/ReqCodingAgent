from __future__ import annotations

from typing import Any

from ..model import ToolDefinition
from ..patching import apply_patch_atomic
from ..workspace import GitWorkspace
from .base import ToolEnvelope, object_schema


def definition() -> ToolDefinition:
    return ToolDefinition(
        "apply_patch",
        "Apply one atomic patch after reading the affected files. Prefer marker format: *** Begin Patch / *** Add File: path / *** Update File: path / *** End Patch. Standard unified diff is also supported and its hunk counts are recounted. If a patch fails, generate a different corrected patch - do not retry the exact same patch.",
        object_schema({"patch": {"type": "string"}}, ["patch"])
    )


def apply_patch(workspace: GitWorkspace, protected_paths: tuple[str, ...], limits: dict[str, int], args: dict[str, Any]) -> ToolEnvelope:
    result = apply_patch_atomic(workspace, args["patch"], limits=limits, protected_paths=protected_paths)
    return ToolEnvelope(True, "apply_patch", {"sha256": result.sha256, "files": result.files, "additions": result.additions, "deletions": result.deletions}, None, False, {})
