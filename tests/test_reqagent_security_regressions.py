from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reqagent.config import AgentConfig
from reqagent.patching import apply_patch_atomic, collect_patch
from reqagent.tools import build_registry
from reqagent.tools.command import ContainerCommandExecutor, LocalTestCommandExecutor
from reqagent.workspace import GitWorkspace, WorkspaceViolation


ROOT = Path(__file__).resolve().parents[1]


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Test")
    (root / "hello.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "protected.txt").write_text("guarded\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    return root


def configuration(protected: list[str] | None = None) -> AgentConfig:
    raw = json.loads((ROOT / "configs/agent/offline-scripted.json").read_text(encoding="utf-8"))
    raw["workspace"]["protected_paths"] = protected or []
    return AgentConfig(raw, ROOT / "configs/agent/offline-scripted.json")


def test_production_registry_fails_closed_without_isolated_executor(tmp_path: Path):
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("secret", encoding="utf-8")
    workspace = GitWorkspace.create(repository(tmp_path))
    registry = build_registry(workspace, configuration().raw, artifact_dir=tmp_path / "commands")

    result = registry.execute(
        "run_command",
        {"command": "python -c \"from pathlib import Path; print(Path('../outside-secret.txt').read_text())\""},
    )

    assert not result.ok
    assert result.error == {"kind": "isolation_unavailable", "message": "isolated command executor is not configured"}
    assert "secret" not in result.to_json()


def test_container_executor_uses_no_network_and_minimum_privileges(tmp_path: Path):
    calls: list[list[str]] = []

    def runner(argv: list[str], **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    workspace = GitWorkspace.create(repository(tmp_path))
    executor = ContainerCommandExecutor(
        command_prefix=("docker",),
        image="local/test:fixed",
        run_id="safe-run",
        runner=runner,
    )
    result = executor.execute(workspace, workspace.root, "python -V", 5, tmp_path / "stdout.log", tmp_path / "stderr.log")

    assert result.exit_code == 0
    command = calls[0]
    assert command[:2] == ["docker", "run"]
    assert "--network" in command and command[command.index("--network") + 1] == "none"
    assert "--cap-drop" in command and command[command.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges" in command
    assert "--privileged" not in command
    assert "--memory" in command and "--pids-limit" in command and "--cpus" in command
    mounts = [command[index + 1] for index, value in enumerate(command) if value == "--mount"]
    assert mounts == [
        f"type=bind,src={workspace.root},dst=/workspace",
        f"type=bind,src={workspace.root / '.git'},dst=/workspace/.git,readonly",
    ]
    assert all(str(workspace.root) in mount for mount in mounts)
    environment_values = [command[index + 1] for index, value in enumerate(command) if value == "--env"]
    assert environment_values == ["HOME=/tmp", "PYTHONDONTWRITEBYTECODE=1"]
    assert not any(value.startswith("PYTHONPATH=") or "API_KEY" in value for value in environment_values)


def test_local_executor_detects_and_restores_protected_path_changes(tmp_path: Path):
    workspace = GitWorkspace.create(repository(tmp_path))
    cfg = configuration(["protected.txt"])
    registry = build_registry(
        workspace,
        cfg.raw,
        command_executor=LocalTestCommandExecutor(),
        artifact_dir=tmp_path / "commands",
    )

    result = registry.execute("run_command", {"command": "printf changed > protected.txt"})

    assert not result.ok
    assert result.error["kind"] == "workspace_violation"
    assert (workspace.root / "protected.txt").read_text(encoding="utf-8") == "guarded\n"


def test_local_executor_records_nonzero_timeout_and_full_output(tmp_path: Path):
    workspace = GitWorkspace.create(repository(tmp_path))
    registry = build_registry(
        workspace,
        configuration().raw,
        command_executor=LocalTestCommandExecutor(),
        artifact_dir=tmp_path / "commands",
    )
    failed = registry.execute("run_command", {"command": "printf bad >&2; exit 7", "timeout_seconds": 2})
    timed_out = registry.execute("run_command", {"command": "sleep 2", "timeout_seconds": 1})

    assert failed.data["exit_code"] == 7 and failed.error["kind"] == "nonzero_exit"
    assert timed_out.data["timed_out"] and timed_out.error["kind"] == "timeout"
    assert (tmp_path / "commands" / "000001.stderr.log").read_text(encoding="utf-8") == "bad"


def test_symlink_patch_failure_leaves_no_symlink(tmp_path: Path):
    workspace = GitWorkspace.create(repository(tmp_path))
    patch = "diff --git a/escape b/escape\nnew file mode 120000\nindex 0000000..7c222fb\n--- /dev/null\n+++ b/escape\n@@ -0,0 +1 @@\n+../outside-secret.txt\n\\ No newline at end of file\n"

    with pytest.raises((ValueError, WorkspaceViolation), match="symlink"):
        apply_patch_atomic(workspace, patch, limits=configuration().workspace)

    assert not (workspace.root / "escape").exists()
    assert not (workspace.root / "escape").is_symlink()


def test_collect_patch_rejects_binary_and_counts_unicode_space_path(tmp_path: Path):
    workspace = GitWorkspace.create(repository(tmp_path))
    binary = workspace.root / "binary.dat"
    binary.write_bytes(b"\x00\x01\x02")
    with pytest.raises(WorkspaceViolation, match="binary"):
        collect_patch(workspace, configuration().workspace)

    binary.unlink()
    named = workspace.root / "空 格.txt"
    named.write_text("hello\n", encoding="utf-8")
    result = collect_patch(workspace, configuration().workspace)
    assert result.files == 1
    assert result.additions == 1
