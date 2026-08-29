from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .workspace import GitWorkspace, WorkspaceViolation


@dataclass(frozen=True)
class PatchResult:
    text: str
    sha256: str
    files: int
    additions: int
    deletions: int
    bytes: int


def _paths(patch: str) -> set[str]:
    result = set()
    for line in patch.splitlines():
        if line.startswith(("+++ ", "--- ")):
            value = line[4:].split("\t", 1)[0]
            if value != "/dev/null":
                result.add(value[2:] if value.startswith(("a/", "b/")) else value)
    return result


def apply_patch_atomic(workspace: GitWorkspace, patch: str, *, protected_paths: tuple[str, ...] = ()) -> PatchResult:
    if not patch.strip() or "GIT binary patch" in patch or "Binary files " in patch:
        raise ValueError("patch must be a non-binary unified diff")
    policy = workspace.policy(protected_paths)
    paths = _paths(patch)
    if not paths:
        raise ValueError("patch contains no file paths")
    for path in paths:
        policy.resolve(path)
    patch_file = workspace.root.parent / "candidate.patch"
    patch_file.write_text(patch.replace("\r\n", "\n"), encoding="utf-8", newline="\n")
    check = subprocess.run(
        ["git", "-C", str(workspace.root), "apply", "--check", "--whitespace=nowarn", str(patch_file)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    if check.returncode:
        raise ValueError(f"patch check failed: {check.stderr.strip()}")
    applied = subprocess.run(
        ["git", "-C", str(workspace.root), "apply", "--whitespace=nowarn", str(patch_file)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    if applied.returncode:
        raise RuntimeError(f"patch apply failed after successful check: {applied.stderr.strip()}")
    for path in paths:
        policy.resolve(path)
    return summarize_patch(workspace.diff())


def summarize_patch(patch: str) -> PatchResult:
    additions = deletions = 0
    for line in patch.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
    return PatchResult(
        text=patch,
        sha256=hashlib.sha256(patch.encode("utf-8")).hexdigest(),
        files=len(_paths(patch)),
        additions=additions,
        deletions=deletions,
        bytes=len(patch.encode("utf-8")),
    )


def collect_patch(workspace: GitWorkspace, limits: dict[str, int], *, protected_paths: tuple[str, ...] = ()) -> PatchResult:
    patch = workspace.diff()
    result = summarize_patch(patch)
    for path in _paths(patch):
        workspace.policy(protected_paths).resolve(path)
    if result.files > limits["max_patch_files"]:
        raise WorkspaceViolation("patch file limit exceeded")
    if result.additions + result.deletions > limits["max_patch_lines"]:
        raise WorkspaceViolation("patch line limit exceeded")
    if result.bytes > limits["max_patch_bytes"]:
        raise WorkspaceViolation("patch byte limit exceeded")
    return result
