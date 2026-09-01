from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
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


def _git_snapshot(workspace: GitWorkspace) -> tuple[str, list[str], int, int, bool, list[str]]:
    with tempfile.TemporaryDirectory(prefix="reqagent-index-") as temporary:
        index = Path(temporary) / "index"
        env = {**os.environ, "GIT_INDEX_FILE": str(index)}
        prefix = ["git", "-C", str(workspace.root)]
        subprocess.run([*prefix, "read-tree", workspace.base_commit], env=env, check=True, capture_output=True)
        subprocess.run([*prefix, "add", "-A", "--", "."], env=env, check=True, capture_output=True)
        patch = subprocess.run(
            [*prefix, "diff", "--cached", "--binary", "--no-ext-diff", workspace.base_commit, "--"],
            env=env, check=True, capture_output=True,
        ).stdout.decode("utf-8", errors="surrogateescape").replace("\r\n", "\n")
        raw_names = subprocess.run(
            [*prefix, "diff", "--cached", "--name-only", "-z", workspace.base_commit, "--"],
            env=env, check=True, capture_output=True,
        ).stdout
        paths = [value.decode("utf-8", errors="surrogateescape") for value in raw_names.split(b"\0") if value]
        raw_numstat = subprocess.run(
            [*prefix, "diff", "--cached", "--numstat", "-z", workspace.base_commit, "--"],
            env=env, check=True, capture_output=True,
        ).stdout
        raw_modes = subprocess.run(
            [*prefix, "diff", "--cached", "--raw", "-z", workspace.base_commit, "--"],
            env=env, check=True, capture_output=True,
        ).stdout
    additions = deletions = 0
    binary = False
    for record in raw_numstat.split(b"\0"):
        if not record:
            continue
        fields = record.split(b"\t", 2)
        if len(fields) < 2:
            continue
        if fields[0] == b"-" or fields[1] == b"-":
            binary = True
        else:
            additions += int(fields[0])
            deletions += int(fields[1])
    symlinks = []
    raw_records = raw_modes.split(b"\0")
    for index in range(0, len(raw_records) - 1, 2):
        header = raw_records[index].decode("ascii", errors="replace")
        path = raw_records[index + 1].decode("utf-8", errors="surrogateescape")
        fields = header.split()
        if len(fields) >= 2 and (fields[0] == ":120000" or fields[1] == "120000"):
            symlinks.append(path)
    return patch, paths, additions, deletions, binary, symlinks


def _validate_result(
    workspace: GitWorkspace,
    limits: dict[str, int],
    protected_paths: tuple[str, ...],
) -> PatchResult:
    patch, paths, additions, deletions, binary, symlinks = _git_snapshot(workspace)
    if binary or "GIT binary patch" in patch or "Binary files " in patch:
        raise WorkspaceViolation("binary patch is forbidden")
    if symlinks:
        raise WorkspaceViolation("symlink patch is forbidden")
    policy = workspace.policy(protected_paths)
    for path in paths:
        policy.resolve(path)
    result = PatchResult(
        text=patch,
        sha256=hashlib.sha256(patch.encode("utf-8", errors="surrogateescape")).hexdigest(),
        files=len(paths),
        additions=additions,
        deletions=deletions,
        bytes=len(patch.encode("utf-8", errors="surrogateescape")),
    )
    if result.files > limits["max_patch_files"]:
        raise WorkspaceViolation("patch file limit exceeded")
    if result.additions + result.deletions > limits["max_patch_lines"]:
        raise WorkspaceViolation("patch line limit exceeded")
    if result.bytes > limits["max_patch_bytes"]:
        raise WorkspaceViolation("patch byte limit exceeded")
    return result


def _snapshot_tree(root: Path) -> Path:
    backup = Path(tempfile.mkdtemp(prefix="reqagent-patch-backup-")) / "tree"
    shutil.copytree(root, backup, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    return backup


def _restore_tree(root: Path, backup: Path) -> None:
    for child in root.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink(missing_ok=True)
    for child in backup.iterdir():
        destination = root / child.name
        if child.is_dir() and not child.is_symlink():
            shutil.copytree(child, destination, symlinks=True)
        elif child.is_symlink():
            destination.symlink_to(os.readlink(child), target_is_directory=child.is_dir())
        else:
            shutil.copy2(child, destination)


def _apply_marker_patch(workspace: GitWorkspace, patch: str, protected_paths: tuple[str, ...]) -> bool:
    lines = patch.replace("\r\n", "\n").splitlines()
    if not lines or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        return False
    policy = workspace.policy(protected_paths)
    index = 1
    while index < len(lines) - 1:
        header = lines[index]
        if header.startswith("*** Update File: "):
            path = header.removeprefix("*** Update File: ")
            target = policy.resolve(path, must_exist=True)
            original = target.read_text(encoding="utf-8")
            index += 1
            if index < len(lines) - 1 and lines[index].startswith("@@"):
                index += 1
            old_parts: list[str] = []
            new_parts: list[str] = []
            while index < len(lines) - 1 and not lines[index].startswith("*** "):
                line = lines[index]
                if line.startswith("-"):
                    old_parts.append(line[1:])
                elif line.startswith("+"):
                    new_parts.append(line[1:])
                else:
                    value = line[1:] if line.startswith(" ") else line
                    old_parts.append(value)
                    new_parts.append(value)
                index += 1
            old = "\n".join(old_parts)
            new = "\n".join(new_parts)
            if old not in original or original.count(old) != 1:
                raise ValueError(f"patch check failed: update context is not unique in {path}")
            target.write_text(original.replace(old, new, 1), encoding="utf-8", newline="\n")
            continue
        if header.startswith("*** Delete File: "):
            policy.resolve(header.removeprefix("*** Delete File: "), must_exist=True).unlink()
            index += 1
            continue
        if header.startswith("*** Add File: "):
            path = header.removeprefix("*** Add File: ")
            target = policy.resolve(path)
            if target.exists() or target.is_symlink():
                raise ValueError(f"patch check failed: add target exists: {path}")
            index += 1
            content = []
            while index < len(lines) - 1 and not lines[index].startswith("*** "):
                if not lines[index].startswith("+"):
                    raise ValueError(f"patch check failed: invalid add line for {path}")
                content.append(lines[index][1:])
                index += 1
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("\n".join(content) + "\n", encoding="utf-8", newline="\n")
            continue
        raise ValueError(f"patch check failed: unsupported marker line {header}")
    return True


def apply_patch_atomic(
    workspace: GitWorkspace,
    patch: str,
    *,
    limits: dict[str, int] | None = None,
    protected_paths: tuple[str, ...] = (),
) -> PatchResult:
    if not patch.strip() or "GIT binary patch" in patch or "Binary files " in patch:
        raise ValueError("patch must be a non-binary unified diff")
    if "new file mode 120000" in patch or "old mode 120000" in patch or "new mode 120000" in patch:
        raise WorkspaceViolation("symlink patch is forbidden")
    effective_limits = limits or {"max_patch_files": 5, "max_patch_lines": 500, "max_patch_bytes": 131072}
    backup = _snapshot_tree(workspace.root)
    patch_file = Path(tempfile.mkdtemp(prefix="reqagent-patch-")) / "candidate.patch"
    try:
        if not _apply_marker_patch(workspace, patch, protected_paths):
            patch_file.write_text(patch.replace("\r\n", "\n"), encoding="utf-8", newline="\n")
            check = subprocess.run(
                ["git", "-C", str(workspace.root), "apply", "--check", "--recount", "--whitespace=nowarn", str(patch_file)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
            )
            if check.returncode:
                raise ValueError(f"patch check failed: {check.stderr.strip()}")
            applied = subprocess.run(
                ["git", "-C", str(workspace.root), "apply", "--recount", "--whitespace=nowarn", str(patch_file)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
            )
            if applied.returncode:
                raise ValueError(f"patch apply failed: {applied.stderr.strip()}")
        return _validate_result(workspace, effective_limits, protected_paths)
    except Exception:
        _restore_tree(workspace.root, backup)
        raise
    finally:
        shutil.rmtree(backup.parent, ignore_errors=True)
        shutil.rmtree(patch_file.parent, ignore_errors=True)


def summarize_patch(patch: str) -> PatchResult:
    additions = sum(1 for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
    deletions = sum(1 for line in patch.splitlines() if line.startswith("-") and not line.startswith("---"))
    files = sum(1 for line in patch.splitlines() if line.startswith("diff --git "))
    return PatchResult(patch, hashlib.sha256(patch.encode("utf-8")).hexdigest(), files, additions, deletions, len(patch.encode("utf-8")))


def collect_patch(workspace: GitWorkspace, limits: dict[str, int], *, protected_paths: tuple[str, ...] = ()) -> PatchResult:
    return _validate_result(workspace, limits, protected_paths)
