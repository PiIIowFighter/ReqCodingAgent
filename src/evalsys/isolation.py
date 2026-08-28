from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .errors import EvalError
from .preflight import PROBE_IMAGE

_PUBLIC_PROMPT_KEYS = ("case_id", "prompt_variant", "prompt", "prompt_sha256")
_FORBIDDEN_NAMES = {
    "benchmark", "private", "oracle", "oracles", "gold.patch", "test.patch", "hints", "hints.txt",
    "计划", "资料", "artifacts", "logs", "cache", ".cache",
}
_CONTAINER_PROBE = r'''set -eu
[ -f /workspace/repo/.evalsys-allowed-repo ]
[ -f /workspace/prompt.json ]
[ -f /workspace/workspace-manifest.json ]
for path in /workspace/benchmark/private /workspace/Oracle /workspace/oracle /workspace/gold.patch /workspace/test.patch /workspace/hints /workspace/计划 /workspace/资料 /workspace/artifacts /workspace/logs /workspace/cache /workspace/.cache; do
  [ ! -e "$path" ] || exit 21
done
if grep -R -E 'EVALSYS_PRIVATE_CANARY_[A-Za-z0-9_]+' /workspace >/dev/null 2>&1; then exit 22; fi
printf '%s' '{"positive":true,"negative":true}'
'''


class ContainerRunner:
    def run(self, argv: list[str], *, timeout_s: int = 30) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(argv, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout_s)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess(argv, 127, "", str(exc))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_safe_task_repo(task_repo: Path, project_root: Path) -> None:
    source = task_repo.resolve()
    root = project_root.resolve()
    try:
        source.relative_to(root)
    except ValueError:
        pass
    else:
        raise EvalError("Task repository must not be the evaluator project root or live inside it", hint="Use a clean external task repository fixture")
    if not source.is_dir():
        raise EvalError("Clean task repository fixture is missing", hint="Prepare the task checkout outside the evaluator project")
    for path in source.rglob("*"):
        if path.is_symlink():
            raise EvalError(f"Task repository contains a symlink: {path.name}", hint="Use a clean fixture without links that can escape the workspace")
        if path.name.casefold() in {name.casefold() for name in _FORBIDDEN_NAMES}:
            raise EvalError(f"Task repository contains forbidden evaluator-like entry: {path.name}", hint="Remove private evaluator data from the clean task fixture")


def construct_agent_workspace(task_repo: Path, public_case: dict[str, Any], destination: Path, *, project_root: Path) -> dict[str, Any]:
    _assert_safe_task_repo(task_repo, project_root)
    target = destination.resolve()
    root = project_root.resolve()
    try:
        target.relative_to(root)
    except ValueError:
        pass
    else:
        raise EvalError("Agent workspace must be outside the evaluator project root", hint="Use an external temporary workspace root")
    if target.exists():
        raise EvalError("Agent workspace destination already exists", hint="Use a fresh destination to prevent stale private files")
    missing = [key for key in _PUBLIC_PROMPT_KEYS if key not in public_case]
    if missing or public_case.get("prompt_variant") not in {"full", "fuzzy"}:
        raise EvalError(f"Invalid public prompt record; missing={missing}", hint="Pass exactly one validated public full or fuzzy record")
    target.mkdir(parents=True)
    repo_target = target / "repo"
    shutil.copytree(task_repo, repo_target)
    (repo_target / ".evalsys-allowed-repo").write_text("allowed\n", encoding="utf-8")
    prompt_record = {key: public_case[key] for key in _PUBLIC_PROMPT_KEYS}
    prompt_path = target / "prompt.json"
    prompt_path.write_text(json.dumps(prompt_record, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    manifest = {
        "schema_version": "1.0",
        "workspace_layout": ["repo", "prompt.json", "workspace-manifest.json"],
        "prompt_variants": [public_case["prompt_variant"]],
        "case_id": public_case["case_id"],
        "prompt_file_sha256": _sha256(prompt_path),
        "source_project_root_mounted": False,
        "mount_allowlist": ["workspace"],
    }
    (target / "workspace-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def _host_probe(workspace: Path, project_root: Path) -> dict[str, bool]:
    positive = (workspace / "repo/.evalsys-allowed-repo").is_file() and (workspace / "prompt.json").is_file()
    forbidden_paths = [
        "benchmark/private", "Oracle", "oracle", "gold.patch", "test.patch", "hints", "计划", "资料",
        "artifacts", "logs", "cache", ".cache",
    ]
    no_paths = all(not (workspace / relative).exists() for relative in forbidden_paths)
    workspace_bytes = b"\n".join(path.read_bytes() for path in workspace.rglob("*") if path.is_file())
    private_canaries = []
    for path in project_root.rglob("*"):
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            private_canaries.extend(token for token in text.split() if token.startswith("EVALSYS_PRIVATE_CANARY_"))
    no_canaries = all(canary.encode() not in workspace_bytes for canary in private_canaries)
    return {"positive": positive, "negative": no_paths and no_canaries}


def prove_isolation(
    project_root: Path,
    task_repo: Path,
    public_case: dict[str, Any],
    destination: Path,
    *,
    runner: ContainerRunner | None = None,
    docker_prefix: list[str] | None = None,
    platform_name: str | None = None,
) -> dict[str, Any]:
    manifest = construct_agent_workspace(task_repo, public_case, destination, project_root=project_root)
    host = _host_probe(destination, project_root)
    active_runner = runner or ContainerRunner()
    current_platform = platform_name or sys.platform
    if current_platform == "win32":
        converted = active_runner.run(["wsl.exe", "--", "wslpath", "-a", destination.resolve().as_posix()])
        if converted.returncode != 0 or not converted.stdout.strip():
            raise EvalError("WSL2 could not translate the isolated workspace path", hint="Enable WSL drive integration and retry", category="infra_failed")
        source = converted.stdout.strip()
        prefix = docker_prefix or ["wsl.exe", "--", "docker"]
    else:
        source = str(destination.resolve())
        prefix = docker_prefix or ["docker"]
    mount = f"type=bind,source={source},target=/workspace,readonly"
    command = [*prefix, "run", "--rm", "--network", "none", "--mount", mount, PROBE_IMAGE, "sh", "-c", _CONTAINER_PROBE]
    result = active_runner.run(command)
    try:
        container = json.loads(result.stdout) if result.returncode == 0 else {"positive": False, "negative": False}
    except json.JSONDecodeError:
        container = {"positive": False, "negative": False}
    passed = host == {"positive": True, "negative": True} and container == {"positive": True, "negative": True}
    if not passed:
        raise EvalError("Agent isolation executable probes failed", hint="Inspect the private local container stderr and ensure only the workspace mount is allowed", category="infra_failed")
    return {
        "schema_version": "1.0",
        "status": "passed",
        "workspace_manifest_sha256": _sha256(destination / "workspace-manifest.json"),
        "prompt_file_sha256": manifest["prompt_file_sha256"],
        "host_probe": host,
        "container_probe": container,
        "positive_probe_categories": ["task_repo", "single_public_prompt"],
        "negative_probe_categories": ["benchmark_private", "oracle", "gold_patch", "test_patch", "hints", "plan", "materials", "evaluator_logs", "evaluator_cache", "private_canaries"],
        "container_mount_count": 1,
        "project_root_mounted": False,
        "forbidden_mounts": [],
        "forbidden_allowlist_entries": [],
        "sanitized": True,
    }
