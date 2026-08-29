from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class WorkspaceViolation(ValueError):
    pass


def _run_git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


@dataclass(frozen=True)
class WorkspacePolicy:
    root: Path
    protected_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.resolve())

    def resolve(self, raw: str, *, must_exist: bool = False) -> Path:
        if not isinstance(raw, str) or not raw.strip():
            raise WorkspaceViolation("path must be a non-empty relative path")
        normalized = raw.replace("\\", "/")
        path = PurePosixPath(normalized)
        if Path(raw).is_absolute() or path.is_absolute() or path.drive or (len(normalized) >= 2 and normalized[1] == ":") or ".." in path.parts:
            raise WorkspaceViolation("absolute paths and traversal are forbidden")
        if any(part.lower() == ".git" for part in path.parts):
            raise WorkspaceViolation(".git is protected")
        candidate = self.root.joinpath(*path.parts)
        resolved = candidate.resolve(strict=must_exist)
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceViolation("path escapes workspace") from exc
        relative = resolved.relative_to(self.root).as_posix()
        for protected in self.protected_paths:
            clean = protected.strip("/")
            if relative == clean or relative.startswith(clean + "/"):
                raise WorkspaceViolation("path is protected")
        return resolved


class GitWorkspace:
    def __init__(self, source: Path, root: Path, base_commit: str, *, temporary_root: Path | None = None):
        self.source = source.resolve()
        self.root = root.resolve()
        self.base_commit = base_commit
        self.temporary_root = temporary_root

    @classmethod
    def create(cls, source: Path, *, destination: Path | None = None) -> "GitWorkspace":
        source = source.resolve()
        if _run_git(source, "rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
            raise ValueError("workspace source must be a Git repository")
        status = _run_git(source, "status", "--porcelain").stdout
        if status.strip():
            raise ValueError("workspace source must be clean")
        base = _run_git(source, "rev-parse", "HEAD").stdout.strip()
        if destination is None:
            temporary = Path(tempfile.mkdtemp(prefix="reqagent-workspace-"))
            destination = temporary / "repo"
        else:
            temporary = destination.parent
            destination.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            ["git", "clone", "--quiet", "--no-hardlinks", str(source), str(destination)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode:
            raise ValueError(f"cannot create isolated workspace: {completed.stderr.strip()}")
        _run_git(destination, "checkout", "--detach", "--quiet", base)
        return cls(source, destination, base, temporary_root=temporary)

    def policy(self, protected_paths: list[str] | tuple[str, ...] = ()) -> WorkspacePolicy:
        return WorkspacePolicy(self.root, tuple(protected_paths))

    def diff(self) -> str:
        _run_git(self.root, "add", "--intent-to-add", "--all")
        return _run_git(self.root, "diff", "--no-ext-diff", self.base_commit, "--").stdout.replace("\r\n", "\n")

    def diff_hash(self) -> str:
        return hashlib.sha256(self.diff().encode("utf-8")).hexdigest()

    def tracked_status(self) -> str:
        return _run_git(self.root, "status", "--porcelain=v1", "--untracked-files=all").stdout

    def cleanup(self) -> None:
        if self.temporary_root and self.temporary_root.exists():
            shutil.rmtree(self.temporary_root, ignore_errors=True)
