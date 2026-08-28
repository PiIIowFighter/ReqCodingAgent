from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from evalsys.config import Settings
from evalsys.errors import EvalError
from evalsys.preflight import CommandRunner, run_preflight


def _settings(tmp_path: Path) -> Settings:
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "external-cache"
    artifacts = project / "artifacts"
    return Settings(project, cache, artifacts)


class FakeRunner(CommandRunner):
    def __init__(self, *, docker_os: str = "linux", machine: str = "x86_64", fail: str | None = None) -> None:
        self.docker_os = docker_os
        self.machine = machine
        self.fail = fail
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], *, timeout_s: int = 30) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        joined = " ".join(argv)
        if self.fail and self.fail in joined:
            return subprocess.CompletedProcess(argv, 1, "", "simulated failure")
        if "wslpath" in joined:
            return subprocess.CompletedProcess(argv, 0, str(argv[-1]), "")
        if "python3.11 --version" in joined:
            return subprocess.CompletedProcess(argv, 0, "Python 3.11.16", "")
        if "docker version" in joined:
            return subprocess.CompletedProcess(argv, 0, f'{{"Os":"{self.docker_os}","Arch":"{self.machine}","Version":"27.0"}}', "")
        if "sentinel.txt" in joined:
            return subprocess.CompletedProcess(argv, 0, "evalsys-bind-sentinel", "")
        if "ack.txt" in joined:
            mount = next(value for index, value in enumerate(argv) if argv[index - 1] == "--mount")
            source = Path(mount.split(",source=", 1)[1].split(",target=", 1)[0])
            (source / "ack.txt").write_text("container-write-ok", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")


def test_command_runner_decodes_external_output_without_host_codepage(monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    CommandRunner().run(["docker", "version"])
    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "replace"


def test_preflight_rejects_wrong_python(tmp_path: Path):
    with pytest.raises(EvalError, match="Python 3.11"):
        run_preflight(_settings(tmp_path), runner=FakeRunner(), python_version=(3, 12, 0), verify_locks=lambda _: {})


def test_preflight_reports_missing_docker_with_action(tmp_path: Path):
    with pytest.raises(EvalError, match="Docker daemon is unavailable") as caught:
        run_preflight(_settings(tmp_path), runner=FakeRunner(fail="docker version"), verify_locks=lambda _: {}, python_version=(3, 11, 9), platform_name="linux")
    assert caught.value.category == "infra_failed"


def test_preflight_requires_linux_docker_engine(tmp_path: Path):
    with pytest.raises(EvalError, match="Linux containers"):
        run_preflight(_settings(tmp_path), runner=FakeRunner(docker_os="windows"), verify_locks=lambda _: {}, python_version=(3, 11, 9), platform_name="linux")


def test_preflight_propagates_source_lock_failure(tmp_path: Path):
    def fail_locks(_: Settings):
        raise EvalError("locked harness checkout is missing", hint="checkout exact revisions")

    with pytest.raises(EvalError, match="locked harness"):
        run_preflight(_settings(tmp_path), runner=FakeRunner(), verify_locks=fail_locks, python_version=(3, 11, 9), platform_name="linux")


def test_preflight_executes_chinese_space_bind_read_and_write(tmp_path: Path):
    runner = FakeRunner()
    report = run_preflight(_settings(tmp_path), runner=runner, verify_locks=lambda _: {"harness": "a" * 40}, python_version=(3, 11, 9), platform_name="linux")
    mounts = [argument for call in runner.calls for argument in call if ",source=" in argument]
    assert report["status"] == "passed"
    assert report["bind_mount"]["host_to_container"] is True
    assert report["bind_mount"]["container_to_host"] is True
    assert any("中文 路径" in mount for mount in mounts)
    assert all(isinstance(call, list) for call in runner.calls)


def test_windows_uses_wsl_docker_path(tmp_path: Path):
    runner = FakeRunner()
    report = run_preflight(
        _settings(tmp_path),
        runner=runner,
        verify_locks=lambda _: {},
        platform_name="win32",
        python_version=(3, 11, 9),
    )
    assert report["docker_transport"] == "wsl2"
    wslpath_call = next(call for call in runner.calls if "wslpath" in call)
    assert "\\" not in wslpath_call[-1]
    assert all(call[:3] in (["wsl.exe", "--", "docker"], ["wsl.exe", "--", "wslpath"], ["wsl.exe", "--", "python3.11"]) or call[:2] == ["wsl.exe", "--status"] for call in runner.calls)
