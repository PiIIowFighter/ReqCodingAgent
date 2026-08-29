from __future__ import annotations

import hashlib
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from ..model import ToolDefinition
from ..workspace import GitWorkspace
from .base import ToolEnvelope, object_schema


@dataclass(frozen=True)
class CommandExecution:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    elapsed_seconds: float


class CommandExecutor(Protocol):
    def execute(
        self,
        workspace: GitWorkspace,
        cwd: Path,
        command: str,
        timeout: int,
        stdout_path: Path,
        stderr_path: Path,
    ) -> CommandExecution: ...


class UnavailableCommandExecutor:
    def execute(
        self,
        workspace: GitWorkspace,
        cwd: Path,
        command: str,
        timeout: int,
        stdout_path: Path,
        stderr_path: Path,
    ) -> CommandExecution:
        del workspace, cwd, command, timeout, stdout_path, stderr_path
        raise IsolationUnavailable("isolated command executor is not configured")


class IsolationUnavailable(RuntimeError):
    pass


class LocalTestCommandExecutor:
    """Host executor available only through explicit test dependency injection."""

    def execute(
        self,
        workspace: GitWorkspace,
        cwd: Path,
        command: str,
        timeout: int,
        stdout_path: Path,
        stderr_path: Path,
    ) -> CommandExecution:
        del workspace
        bash = shutil.which("bash")
        git = shutil.which("git")
        if os.name == "nt" and git:
            candidate = Path(git).with_name("bash.exe")
            if candidate.is_file():
                bash = str(candidate)
        if not bash:
            raise IsolationUnavailable("test shell is unavailable")
        env = {
            "PATH": os.environ.get("PATH", ""),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        }
        started = time.monotonic()
        process = subprocess.Popen(
            [bash, "-lc", command], cwd=cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            start_new_session=os.name != "nt",
        )
        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        stdout_path.write_text(stdout, encoding="utf-8", newline="\n")
        stderr_path.write_text(stderr, encoding="utf-8", newline="\n")
        return CommandExecution(process.returncode, stdout, stderr, timed_out, time.monotonic() - started)


class ContainerCommandExecutor:
    def __init__(
        self,
        *,
        command_prefix: tuple[str, ...],
        image: str,
        run_id: str,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        memory: str = "2g",
        cpus: str = "2",
        pids_limit: int = 256,
        path_converter: Callable[[Path], str] | None = None,
    ):
        if not command_prefix or not image or not run_id:
            raise ValueError("container command prefix, image, and run id are required")
        self.command_prefix = command_prefix
        self.image = image
        self.run_id = run_id
        self.runner = runner
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.path_converter = path_converter or (lambda path: str(path))

    def execute(
        self,
        workspace: GitWorkspace,
        cwd: Path,
        command: str,
        timeout: int,
        stdout_path: Path,
        stderr_path: Path,
    ) -> CommandExecution:
        relative_cwd = cwd.relative_to(workspace.root).as_posix()
        container_name = f"reqagent-{self.run_id}"
        mount_root = self.path_converter(workspace.root)
        git_mount = self.path_converter(workspace.root / ".git")
        argv = [
            *self.command_prefix, "run", "--pull", "never", "--rm", "--name", container_name,
            "--network", "none", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--memory", self.memory,
            "--cpus", self.cpus, "--pids-limit", str(self.pids_limit),
            "--mount", f"type=bind,src={mount_root},dst=/workspace",
            "--mount", f"type=bind,src={git_mount},dst=/workspace/.git,readonly",
            "--workdir", "/workspace" if relative_cwd == "." else f"/workspace/{relative_cwd}",
            "--env", "HOME=/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1",
            self.image, "bash", "-lc", command,
        ]
        started = time.monotonic()
        try:
            completed = self.runner(
                argv, capture_output=True, text=True, encoding="utf-8",
                errors="replace", check=False, timeout=timeout,
            )
            stdout, stderr = completed.stdout, completed.stderr
            execution = CommandExecution(completed.returncode, stdout, stderr, False, time.monotonic() - started)
        except subprocess.TimeoutExpired as exc:
            self.runner(
                [*self.command_prefix, "rm", "-f", container_name],
                capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
            )
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            execution = CommandExecution(-9, stdout, stderr, True, time.monotonic() - started)
        stdout_path.write_text(stdout, encoding="utf-8", newline="\n")
        stderr_path.write_text(stderr, encoding="utf-8", newline="\n")
        return execution


def definition() -> ToolDefinition:
    return ToolDefinition(
        "run_command",
        "Run a repository command in the configured isolated container with no network and bounded resources.",
        object_schema(
            {"command": {"type": "string"}, "cwd": {"type": "string"}, "timeout_seconds": {"type": "integer", "minimum": 1}},
            ["command"],
        ),
    )


def _truncate(value: str, limit: int = 65536) -> tuple[str, bool]:
    raw = value.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return value, False
    half = limit // 2
    return raw[:half].decode("utf-8", errors="ignore") + "\n...[truncated]...\n" + raw[-half:].decode("utf-8", errors="ignore"), True


def _fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    if not path.exists() and not path.is_symlink():
        digest.update(b"missing")
    elif path.is_symlink():
        digest.update(b"symlink\0" + os.readlink(path).encode("utf-8", errors="surrogateescape"))
    elif path.is_file():
        digest.update(b"file\0" + path.read_bytes())
    else:
        digest.update(b"directory\0")
        for child in sorted(path.rglob("*")):
            digest.update(child.relative_to(path).as_posix().encode("utf-8"))
            if child.is_file() and not child.is_symlink():
                digest.update(child.read_bytes())
    return digest.hexdigest()


def run_command(
    workspace: GitWorkspace,
    protected_paths: tuple[str, ...],
    max_timeout: int,
    executor: CommandExecutor,
    artifact_dir: Path,
    sequence: int,
    args: dict[str, Any],
) -> ToolEnvelope:
    cwd = workspace.policy(protected_paths).resolve(args.get("cwd", "."), must_exist=True)
    if not cwd.is_dir():
        raise ValueError("cwd is not a directory")
    timeout = min(args.get("timeout_seconds", max_timeout), max_timeout)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = artifact_dir / f"{sequence:06d}.stdout.log"
    stderr_path = artifact_dir / f"{sequence:06d}.stderr.log"
    watched = (".git", *protected_paths)
    before = {path: _fingerprint(workspace.root / path) for path in watched}
    try:
        execution = executor.execute(workspace, cwd, args["command"], timeout, stdout_path, stderr_path)
    except IsolationUnavailable as exc:
        return ToolEnvelope(False, "run_command", {}, {"kind": "isolation_unavailable", "message": str(exc)}, False, {})
    changed = [path for path in watched if _fingerprint(workspace.root / path) != before[path]]
    if changed:
        workspace.restore_paths(changed)
        return ToolEnvelope(
            False, "run_command", {"changed_protected_paths": changed},
            {"kind": "workspace_violation", "message": "command modified protected workspace paths"}, False,
            {"stdout_artifact": stdout_path.name, "stderr_artifact": stderr_path.name},
        )
    stdout, out_cut = _truncate(execution.stdout)
    stderr, err_cut = _truncate(execution.stderr)
    failed = execution.timed_out or execution.exit_code != 0
    return ToolEnvelope(
        not failed,
        "run_command",
        {"command": args["command"], "exit_code": execution.exit_code, "stdout": stdout, "stderr": stderr, "timed_out": execution.timed_out},
        {"kind": "timeout" if execution.timed_out else "nonzero_exit", "message": "command timed out" if execution.timed_out else f"command exited {execution.exit_code}"} if failed else None,
        out_cut or err_cut,
        {"elapsed_seconds": round(execution.elapsed_seconds, 3), "stdout_artifact": stdout_path.name, "stderr_artifact": stderr_path.name},
    )
