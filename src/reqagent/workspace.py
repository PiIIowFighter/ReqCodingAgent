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


def _git_toplevel(path: Path) -> Path | None:
    completed = _run_git(path, "rev-parse", "--show-toplevel", check=False)
    if completed.returncode:
        return None
    return Path(completed.stdout.strip()).resolve()


def _ensure_git_baseline(root: Path) -> str:
    root = root.resolve()
    if not (root / ".git").exists():
        _run_git(root, "init", "--quiet")
    if _run_git(root, "rev-parse", "HEAD", check=False).returncode:
        _run_git(root, "add", "-A")
        _run_git(root, "-c", "user.email=reqagent@local", "-c", "user.name=ReqAgent",
                 "commit", "--quiet", "--allow-empty", "-m", "reqagent-baseline")
    elif _run_git(root, "status", "--porcelain").stdout.strip():
        _run_git(root, "add", "-A")
        _run_git(root, "-c", "user.email=reqagent@local", "-c", "user.name=ReqAgent",
                 "commit", "--quiet", "-m", "reqagent-baseline")
    return _run_git(root, "rev-parse", "HEAD").stdout.strip()


class GitWorkspace:
    def __init__(self, source: Path, root: Path, base_commit: str, *, temporary_root: Path | None = None):
        self.source = source.resolve()
        self.root = root.resolve()
        self.base_commit = base_commit
        self.temporary_root = temporary_root

    @classmethod
    def create(cls, source: Path, *, destination: Path | None = None, in_place: bool = False) -> "GitWorkspace":
        source = source.resolve()
        if not source.is_dir():
            raise ValueError("workspace source must be an existing directory")
        if in_place:
            base = _ensure_git_baseline(source)
            return cls(source, source, base, temporary_root=None)
        if destination is None:
            temporary = Path(tempfile.mkdtemp(prefix="reqagent-workspace-"))
            destination = temporary / "repo"
        else:
            temporary = destination.parent
            destination.parent.mkdir(parents=True, exist_ok=True)
        if _git_toplevel(source) == source:
            status = _run_git(source, "status", "--porcelain").stdout
            if status.strip():
                raise ValueError("workspace source must be clean")
            base = _run_git(source, "rev-parse", "HEAD").stdout.strip()
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
        shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=False, ignore=shutil.ignore_patterns(".git"))
        _run_git(destination, "init", "--quiet")
        _run_git(destination, "add", "-A")
        _run_git(destination, "-c", "user.email=reqagent@local", "-c", "user.name=ReqAgent",
                 "commit", "--quiet", "--allow-empty", "-m", "baseline")
        base = _run_git(destination, "rev-parse", "HEAD").stdout.strip()
        return cls(source, destination, base, temporary_root=temporary)

    def policy(self, protected_paths: list[str] | tuple[str, ...] = ()) -> WorkspacePolicy:
        return WorkspacePolicy(self.root, tuple(protected_paths))

    def diff(self) -> str:
        with tempfile.TemporaryDirectory(prefix="reqagent-index-") as temporary:
            env = {**os.environ, "GIT_INDEX_FILE": str(Path(temporary) / "index")}
            prefix = ["git", "-C", str(self.root)]
            subprocess.run([*prefix, "read-tree", self.base_commit], env=env, check=True, capture_output=True)
            subprocess.run([*prefix, "add", "-A", "--", "."], env=env, check=True, capture_output=True)
            completed = subprocess.run(
                [*prefix, "diff", "--cached", "--no-ext-diff", self.base_commit, "--"],
                env=env, check=True, capture_output=True,
            )
        return completed.stdout.decode("utf-8", errors="surrogateescape").replace("\r\n", "\n")

    def diff_hash(self) -> str:
        return hashlib.sha256(self.diff().encode("utf-8")).hexdigest()

    def tracked_status(self) -> str:
        return _run_git(self.root, "status", "--porcelain=v1", "--untracked-files=all").stdout

    def protected_fingerprint(self, protected_paths: list[str] | tuple[str, ...]) -> str:
        digest = hashlib.sha256()
        for raw in (".git", *protected_paths):
            path = self.root / raw
            digest.update(raw.encode("utf-8"))
            if not path.exists() and not path.is_symlink():
                digest.update(b"missing")
                continue
            if path.is_symlink():
                digest.update(b"symlink\0" + os.readlink(path).encode("utf-8", errors="surrogateescape"))
                continue
            candidates = [path] if path.is_file() else sorted(path.rglob("*"))
            for candidate in candidates:
                relative = candidate.relative_to(self.root).as_posix()
                digest.update(relative.encode("utf-8"))
                if candidate.is_file() and not candidate.is_symlink():
                    digest.update(candidate.read_bytes())
        return digest.hexdigest()

    def restore_paths(self, paths: list[str] | tuple[str, ...]) -> None:
        for raw in paths:
            normalized = raw.replace("\\", "/").strip("/")
            if normalized == ".git":
                raise WorkspaceViolation(".git was modified and cannot be safely restored")
            candidate = self.root / normalized
            tracked = _run_git(self.root, "ls-files", "--error-unmatch", "--", normalized, check=False).returncode == 0
            if tracked:
                _run_git(self.root, "restore", "--source", self.base_commit, "--staged", "--worktree", "--", normalized)
            elif candidate.is_dir() and not candidate.is_symlink():
                shutil.rmtree(candidate)
            else:
                candidate.unlink(missing_ok=True)

    def cleanup(self) -> None:
        if self.temporary_root and self.temporary_root.exists():
            shutil.rmtree(self.temporary_root, ignore_errors=True)
