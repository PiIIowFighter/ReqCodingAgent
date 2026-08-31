from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from reqagent.tools.command import ContainerCommandExecutor
from reqagent.workspace import GitWorkspace


def test_container_command_bootstraps_existing_testbed_environment_and_records_identity(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "-C", str(source), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
    (source / "x.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "initial"], check=True)
    workspace = GitWorkspace.create(source)
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        stdout = "__REQAGENT_BOOTSTRAP__={\"identity\":\"conda:testbed\",\"interpreter\":\"/opt/miniconda3/envs/testbed/bin/python\",\"pytest_available\":true,\"fallback\":false}\npassed\n"
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    executor = ContainerCommandExecutor(
        command_prefix=("docker",), image="task@sha256:" + "a" * 64,
        run_id="run-1", runner=runner,
    )
    stdout = tmp_path / "stdout.log"
    stderr = tmp_path / "stderr.log"
    result = executor.execute(workspace, workspace.root, "python -m pytest tests/test_x.py", 10, stdout, stderr)

    command = calls[0]
    assert command[command.index("--pull") + 1] == "never"
    assert command[command.index("--network") + 1] == "none"
    shell = command[-1]
    assert "$" not in shell
    assert "base64 -d" in shell
    assert command[command.index("--entrypoint") + 1] == "/bin/bash"
    assert command[-2] == "-c"
    assert command[-3].endswith("@sha256:" + "a" * 64)
    decoded = __import__("base64").b64decode(shell.split("'")[3]).decode("utf-8")
    assert "/opt/miniconda3/envs/testbed/bin/python" in decoded
    assert "/opt/miniconda3/bin/activate" in decoded
    assert 'exec bash -lc "$REQAGENT_COMMAND"' not in decoded
    assert "pip install" not in decoded and "conda install" not in decoded
    assert result.bootstrap == {
        "identity": "conda:testbed",
        "interpreter": "/opt/miniconda3/envs/testbed/bin/python",
        "pytest_available": True,
        "fallback": False,
    }
    assert result.stdout == "passed\n"
