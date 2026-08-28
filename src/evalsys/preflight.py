from __future__ import annotations

import json
import platform
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

from .config import Settings
from .errors import EvalError
from .locks import verify_source_locks

PROBE_IMAGE = "ubuntu@sha256:4f838adc7181d9039ac795a7d0aba05a9bd9ecd480d294483169c5def983b64d"


class CommandRunner:
    def run(self, argv: list[str], *, timeout_s: int = 30) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(argv, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout_s)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess(argv, 127, "", str(exc))


def _checked(runner: CommandRunner, argv: list[str], blocker: str, hint: str) -> subprocess.CompletedProcess[str]:
    result = runner.run(argv)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise EvalError(f"{blocker}: {detail}", hint=hint, category="infra_failed")
    return result


def _docker_prefix(platform_name: str) -> list[str]:
    return ["wsl.exe", "--", "docker"] if platform_name == "win32" else ["docker"]


def resolve_wsl_path(path: Path, runner: CommandRunner) -> str:
    converted = _checked(
        runner,
        ["wsl.exe", "--", "wslpath", "-a", path.as_posix()],
        "WSL2 could not translate the path",
        "Enable WSL integration for the drive containing the project and cache",
    ).stdout.strip()
    if not converted:
        raise EvalError("WSL2 returned an empty path", hint="Run wsl.exe -- wslpath -a <path>", category="infra_failed")
    return converted


def validate_wsl_python(command: str, runner: CommandRunner) -> str:
    result = _checked(runner, ["wsl.exe", "--", command, "--version"], "WSL Python is unavailable", "Set EVALSYS_WSL_PYTHON to an external Python 3.11 environment")
    version = (result.stdout or result.stderr).strip()
    if not version.startswith("Python 3.11."):
        raise EvalError(f"WSL Python 3.11 is required; found {version}", hint="Set EVALSYS_WSL_PYTHON to Python 3.11", category="infra_failed")
    return command


def _docker_source(path: Path, platform_name: str, runner: CommandRunner) -> str:
    return resolve_wsl_path(path, runner) if platform_name == "win32" else str(path)


def _bind_probe(settings: Settings, runner: CommandRunner, platform_name: str) -> dict[str, bool]:
    settings.artifact_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="中文 路径 ", dir=settings.artifact_root) as temporary:
        host = Path(temporary)
        (host / "sentinel.txt").write_text("evalsys-bind-sentinel", encoding="utf-8")
        source = _docker_source(host, platform_name, runner)
        mount = f"type=bind,source={source},target=/probe"
        prefix = _docker_prefix(platform_name)
        read = _checked(
            runner,
            [*prefix, "run", "--rm", "--mount", mount, PROBE_IMAGE, "cat", "/probe/sentinel.txt"],
            "Docker bind mount cannot read the host sentinel",
            "Allow Docker Desktop/WSL2 file sharing for this drive and retry preflight",
        )
        if read.stdout.strip() != "evalsys-bind-sentinel":
            raise EvalError("Docker bind mount returned unexpected sentinel data", hint="Check WSL2 path translation and Docker file sharing", category="infra_failed")
        _checked(
            runner,
            [*prefix, "run", "--rm", "--mount", mount, PROBE_IMAGE, "sh", "-c", "printf container-write-ok > /probe/ack.txt"],
            "Docker bind mount cannot write back to the host",
            "Grant the WSL2 Docker engine read/write access to the external artifact path",
        )
        acknowledgement = host / "ack.txt"
        if not acknowledgement.is_file() or acknowledgement.read_text(encoding="utf-8") != "container-write-ok":
            raise EvalError("Docker write acknowledgement was not visible on the host", hint="Check bind-mount permissions for the artifact directory", category="infra_failed")
    return {"host_to_container": True, "container_to_host": True, "path_shape": "chinese-and-space"}


def run_preflight(
    settings: Settings,
    *,
    perform_bind_test: bool = True,
    runner: CommandRunner | None = None,
    python_version: tuple[int, int, int] | None = None,
    platform_name: str | None = None,
    verify_locks: Callable[[Settings], dict[str, str]] = verify_source_locks,
) -> dict[str, object]:
    runner = runner or CommandRunner()
    version = python_version or sys.version_info[:3]
    current_platform = platform_name or sys.platform
    if version[:2] != (3, 11):
        raise EvalError(f"Python 3.11 is required; found {version[0]}.{version[1]}", hint="Run with py -3.11 or a Python 3.11 virtual environment", category="infra_failed")
    if current_platform == "win32":
        _checked(runner, ["wsl.exe", "--status"], "WSL2 is unavailable", "Install/enable WSL2 and Docker Desktop WSL integration")
        validate_wsl_python(settings.wsl_python, runner)
    settings.cache_root.mkdir(parents=True, exist_ok=True)
    try:
        marker = settings.cache_root / ".evalsys-write-test"
        marker.write_text("ok", encoding="utf-8")
        marker.unlink()
    except OSError as exc:
        raise EvalError(f"External cache is not writable: {exc}", hint="Set EVALSYS_CACHE_ROOT to a writable directory outside the project", category="infra_failed") from exc
    prefix = _docker_prefix(current_platform)
    docker = _checked(
        runner,
        [*prefix, "version", "--format", "{{json .Server}}"],
        "Docker daemon is unavailable",
        "Start Docker Desktop with the WSL2 engine, then verify `wsl.exe -- docker version`" if current_platform == "win32" else "Start Docker, then verify `docker version`",
    )
    try:
        docker_info = json.loads(docker.stdout)
        docker_os, docker_arch, docker_version = (docker_info[key] for key in ("Os", "Arch", "Version"))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise EvalError("Docker server returned an unrecognized version response", hint="Verify Docker server access and retry", category="infra_failed") from exc
    if docker_os.lower() != "linux":
        raise EvalError(f"Docker must run Linux containers; server reports {docker_os}", hint="Switch Docker Desktop to Linux containers", category="infra_failed")
    locks = verify_locks(settings)
    bind = _bind_probe(settings, runner, current_platform) if perform_bind_test else {"performed": False}
    return {
        "schema_version": "1.0",
        "status": "passed",
        "python": ".".join(map(str, version)),
        "wsl_python": settings.wsl_python if current_platform == "win32" else None,
        "host_architecture": platform.machine(),
        "docker": {"os": docker_os, "architecture": docker_arch, "version": docker_version, "probe_image": PROBE_IMAGE},
        "docker_transport": "wsl2" if current_platform == "win32" else "native",
        "source_locks": locks,
        "bind_mount": bind,
    }
