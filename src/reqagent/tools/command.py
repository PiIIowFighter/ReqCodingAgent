from __future__ import annotations

import os
import re
import shlex
import signal
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from ..model import ToolDefinition
from ..workspace import GitWorkspace
from .base import ToolEnvelope, object_schema


def definition() -> ToolDefinition:
    return ToolDefinition("run_command", "Run a repository command with a timeout. Call this for focused tests and diagnostics; network access is unavailable by policy.", object_schema({"command": {"type": "string"}, "cwd": {"type": "string"}, "timeout_seconds": {"type": "integer", "minimum": 1}}, ["command"]))


def _truncate(value: str, limit: int = 65536) -> tuple[str, bool]:
    raw = value.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return value, False
    half = limit // 2
    return (raw[:half].decode("utf-8", errors="ignore") + "\n...[truncated]...\n" + raw[-half:].decode("utf-8", errors="ignore"), True)


def _validate_command(command: str) -> None:
    if not command.strip() or "\x00" in command or "\n" in command or "\r" in command:
        raise ValueError("command must be one non-empty line")
    if re.search(r"(?:^|[;&|\s])(?:curl|wget|ssh|scp|nc|ncat|telnet|powershell|pwsh)(?:\s|$)", command, re.IGNORECASE):
        raise ValueError("network-capable command is forbidden")
    if re.search(r"(?:^|[;&|\s])git\s+(?:push|pull|fetch|clone|remote)(?:\s|$)", command, re.IGNORECASE):
        raise ValueError("network Git command is forbidden")
    if re.search(r"(?:^|\s)(?:/|[A-Za-z]:[/\\])", command):
        raise ValueError("absolute paths in commands are forbidden")
    try:
        shlex.split(command, posix=True)
    except ValueError as exc:
        raise ValueError(f"invalid command quoting: {exc}") from exc


def run_command(workspace: GitWorkspace, max_timeout: int, args: dict[str, Any]) -> ToolEnvelope:
    _validate_command(args["command"])
    cwd = workspace.policy().resolve(args.get("cwd", "."), must_exist=True)
    if not cwd.is_dir():
        raise ValueError("cwd is not a directory")
    timeout = min(args.get("timeout_seconds", max_timeout), max_timeout)
    bash = shutil.which("bash")
    git = shutil.which("git")
    if os.name == "nt" and git:
        candidate = Path(git).with_name("bash.exe")
        if candidate.is_file():
            bash = str(candidate)
    if not bash:
        raise ValueError("bash is unavailable")
    executable = [bash, "-lc", args["command"]]
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "HOME", "LANG", "LC_ALL", "TMP", "TEMP", "PYTHONPATH", "PYTEST_DISABLE_PLUGIN_AUTOLOAD"}}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    started = time.monotonic()
    process = subprocess.Popen(
        executable, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", start_new_session=os.name != "nt",
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
    stdout, out_cut = _truncate(stdout)
    stderr, err_cut = _truncate(stderr)
    return ToolEnvelope(
        not timed_out and process.returncode == 0,
        "run_command",
        {"command": args["command"], "exit_code": process.returncode, "stdout": stdout, "stderr": stderr, "timed_out": timed_out},
        {"kind": "timeout" if timed_out else "nonzero_exit", "message": "command timed out" if timed_out else f"command exited {process.returncode}"} if timed_out or process.returncode else None,
        out_cut or err_cut,
        {"elapsed_seconds": round(time.monotonic() - started, 3)},
    )
