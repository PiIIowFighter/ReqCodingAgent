from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable

from .errors import EvalError
from .evidence import sanitize
from .recovery import sha256_file

CANONICAL_LOCK_SHA256 = "66ada0bfcc5177def68d5307e0c6fdaf5b91b5659258faa1fb2cc4862809d39e"
_REQUIRED_VERSIONS = {"docker": "7.2.0"}
_REQUIRED_PACKAGES = {"docker", "swebench", "datasets", "GitPython", "tqdm", "unidiff", "rich", "requests"}


def _run(runner: Callable[..., subprocess.CompletedProcess[str]], argv: list[str], label: str) -> str:
    completed = runner(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=120)
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()[-500:]
        raise EvalError(f"harness environment {label} failed: {detail}", category="infra_failed")
    return completed.stdout


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _runtime_script() -> str:
    return """import importlib.metadata as m,json,os,site,sys
import docker,swebench,swebench.harness.run_evaluation,datasets,git,tqdm,unidiff,rich,requests
names=['docker','swebench','datasets','GitPython','tqdm','unidiff','rich','requests']
print(json.dumps({'python':'.'.join(map(str,sys.version_info[:3])),'versions':{n:m.version(n) for n in names},'imports':True,'docker_ping':docker.from_env().ping(),'sys_executable':sys.executable,'sys_prefix':sys.prefix,'site_packages':site.getsitepackages(),'no_site':sys.flags.no_site,'distribution':os.environ.get('WSL_DISTRO_NAME','unavailable'),'environment':{n:('set' if os.environ.get(n) else 'unset') for n in ('PYTHONPATH','PYTHONHOME','VIRTUAL_ENV','WSLENV')}},sort_keys=True))"""


def verify_harness_environment(
    project_root: Path,
    checkout: Path,
    python_executable: str,
    *,
    expected_head: str,
    expected_lock_sha256: str = CANONICAL_LOCK_SHA256,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    command_prefix: list[str] | None = None,
    execution_checkout: str | None = None,
) -> dict[str, Any]:
    checkout = checkout.resolve()
    execution_root = execution_checkout or str(checkout)
    expected_python = execution_root.rstrip("/") + "/.venv/bin/python" if execution_checkout else str(checkout / ".venv/bin/python")
    if Path(python_executable) != Path(expected_python):
        raise EvalError("harness interpreter must be checkout .venv/bin/python", category="invalid")
    prefix = list(command_prefix or [])
    def command(*parts: str) -> list[str]:
        return [*prefix, *parts]
    status = _run(runner, command("git", "-C", execution_root, "status", "--porcelain", "--untracked-files=all"), "dirty check")
    if status.strip():
        raise EvalError("harness checkout is dirty", category="invalid")
    head = _run(runner, command("git", "-C", execution_root, "rev-parse", "HEAD"), "HEAD check").strip()
    if head != expected_head:
        raise EvalError("harness checkout HEAD mismatch", category="invalid")
    lock_path = checkout / "uv.lock"
    if not lock_path.is_file() or sha256_file(lock_path) != expected_lock_sha256:
        raise EvalError("harness working lock hash mismatch", category="invalid")
    committed = _run(runner, command("git", "-C", execution_root, "show", "HEAD:uv.lock"), "lock read").encode("utf-8")
    if hashlib.sha256(committed).hexdigest() != expected_lock_sha256 or committed != lock_path.read_bytes():
        raise EvalError("harness lock differs from HEAD", category="invalid")
    runtime_raw = _run(runner, command(python_executable, "-c", _runtime_script(), "runtime-preflight"), "imports and Docker ping")
    try:
        runtime = json.loads(runtime_raw)
    except json.JSONDecodeError as exc:
        raise EvalError("harness imports returned invalid JSON", category="infra_failed") from exc
    if runtime.get("python") != "3.11.16" or runtime.get("imports") is not True or runtime.get("docker_ping") is not True:
        raise EvalError("harness imports or Docker ping failed", category="infra_failed")
    versions = runtime.get("versions", {})
    if not _REQUIRED_PACKAGES.issubset(versions) or any(versions.get(name) != version for name, version in _REQUIRED_VERSIONS.items()):
        raise EvalError("harness imports or package versions mismatch", category="infra_failed")
    expected_relative = ".venv/bin/python"
    if not str(runtime.get("sys_executable", "")).endswith(expected_relative) or runtime.get("no_site") != 0:
        raise EvalError("harness interpreter runtime identity mismatch", category="infra_failed")
    receipt = {
        "schema_version": "1.0", "status": "passed", "checkout_head": head,
        "canonical_lock_sha256": expected_lock_sha256, "checkout_clean": True,
        "interpreter": expected_relative, "python": runtime["python"], "versions": versions,
        "distribution": runtime.get("distribution", "unavailable"), "imports": True,
        "docker_ping": True, "no_site": runtime["no_site"], "environment": runtime.get("environment", {}),
    }
    destination = project_root / "audit/iteration2/harness-environment-receipt.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(sanitize(receipt, project_root=project_root), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    receipt["reference"] = {"path": destination.relative_to(project_root).as_posix(), "sha256": sha256_file(destination)}
    return receipt


def verify_settings_harness_environment(settings) -> dict[str, Any]:
    lock = json.loads((settings.project_root / "benchmark/source-lock.json").read_text(encoding="utf-8"))
    expected_head = lock["sources"]["harness"]["revision"]
    checkout = settings.cache_root / "swe-bench"
    if os.name == "nt":
        converted = subprocess.run(
            ["wsl.exe", "--", "wslpath", "-a", checkout.as_posix()],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=30,
        )
        if converted.returncode or not converted.stdout.strip():
            raise EvalError("canonical harness checkout path cannot be translated to WSL", category="infra_failed")
        execution_checkout = converted.stdout.strip()
        prefix = ["wsl.exe", "--"]
    else:
        execution_checkout = checkout.as_posix()
        prefix = []
    return verify_harness_environment(
        settings.project_root, checkout, settings.wsl_python,
        expected_head=expected_head, runner=subprocess.run, command_prefix=prefix,
        execution_checkout=execution_checkout,
    )


def verify_harness_environment_receipt(project_root: Path, reference: dict[str, str]) -> dict[str, Any]:
    relative = reference.get("path")
    if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise EvalError("harness environment receipt path is invalid", category="invalid")
    path = project_root / relative
    if not path.is_file() or sha256_file(path) != reference.get("sha256"):
        raise EvalError("harness environment receipt SHA-256 mismatch", category="invalid")
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvalError("harness environment receipt is invalid", category="invalid") from exc
    if receipt.get("status") != "passed" or receipt.get("canonical_lock_sha256") != CANONICAL_LOCK_SHA256:
        raise EvalError("harness environment receipt did not pass", category="invalid")
    return receipt
