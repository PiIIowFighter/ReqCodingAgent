from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


class ProcessTimeout(TimeoutError):
    def __init__(self, argv: Sequence[str], timeout_s: int, stdout: str = "", stderr: str = "") -> None:
        super().__init__(f"command timed out after {timeout_s}s: {list(argv)!r}")
        self.argv = list(argv)
        self.timeout_s = timeout_s
        self.stdout = stdout
        self.stderr = stderr


@dataclass(frozen=True)
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str


def run_process(argv: Sequence[str], *, cwd: Path | None = None, timeout_s: int, env: Mapping[str, str] | None = None) -> CommandResult:
    command = [str(part) for part in argv]
    kwargs = {"cwd": cwd, "env": dict(env) if env is not None else None, "text": True, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(command, **kwargs)
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
        else:
            os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
        raise ProcessTimeout(command, timeout_s, stdout or str(exc.stdout or ""), stderr or str(exc.stderr or "")) from exc
    return CommandResult(command, process.returncode, stdout, stderr)
