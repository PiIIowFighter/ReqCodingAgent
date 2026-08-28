from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from .errors import EvalError
from .preflight import PROBE_IMAGE
from .schema import validate_json

_AGENT_PROMPT_KEYS = ("case_id", "prompt_variant", "prompt", "prompt_sha256")
_CANARY = re.compile(r"EVALSYS_PRIVATE_CANARY_[A-Za-z0-9_]+")
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


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _overlap(first: Path, second: Path) -> bool:
    return _contains(first, second) or _contains(second, first)


def _is_reparse(path: Path) -> bool:
    return bool(getattr(path.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _project_canaries(project_root: Path) -> set[str]:
    found: set[str] = set()
    for root, directories, files in os.walk(project_root, followlinks=False):
        directories[:] = [name for name in directories if not _is_reparse(Path(root) / name)]
        for name in files:
            try:
                found.update(_CANARY.findall((Path(root) / name).read_text(encoding="utf-8")))
            except (OSError, UnicodeDecodeError):
                continue
    return found


def _scan_task_repo(source: Path, forbidden_canaries: set[str]) -> None:
    folded = {name.casefold() for name in _FORBIDDEN_NAMES}
    for root, directories, files in os.walk(source, topdown=True, followlinks=False):
        for name in [*directories, *files]:
            path = Path(root) / name
            if path.is_symlink() or _is_reparse(path):
                raise EvalError(f"Task repository contains a symlink or reparse point: {name}", hint="Use a clean fixture without mount-like links")
            try:
                path.resolve().relative_to(source)
            except ValueError as exc:
                raise EvalError(f"Task repository entry escapes its source root: {name}", hint="Remove redirected entries") from exc
            if name.casefold() in folded:
                raise EvalError(f"Task repository contains forbidden evaluator-like entry: {name}", hint="Remove evaluator data")
            if path.is_file():
                try:
                    contents = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                if forbidden_canaries.intersection(_CANARY.findall(contents)):
                    raise EvalError("Task repository contains a private evaluator canary", hint="Recreate the clean task checkout")


def _validate_paths(task_repo: Path, destination: Path, project_root: Path) -> tuple[Path, Path, Path]:
    source, target, root = task_repo.resolve(), destination.resolve(), project_root.resolve()
    if _overlap(source, root):
        raise EvalError("Task repository and evaluator project root overlap or contain one another", hint="Use an external task fixture")
    if _overlap(target, root):
        raise EvalError("Agent workspace and evaluator project root overlap or contain one another", hint="Use an external workspace")
    if _overlap(source, target):
        raise EvalError("Task repository and Agent workspace dangerously overlap or contain one another", hint="Use sibling paths")
    if not source.is_dir():
        raise EvalError("Clean task repository fixture is missing", hint="Prepare the external checkout")
    return source, target, root


def construct_agent_workspace(task_repo: Path, public_case: dict[str, Any], destination: Path, *, project_root: Path) -> dict[str, Any]:
    source, target, root = _validate_paths(task_repo, destination, project_root)
    if target.exists():
        raise EvalError("Agent workspace destination already exists", hint="Use a fresh destination")
    validate_json(public_case, "public-case")
    actual_hash = hashlib.sha256(public_case["prompt"].encode("utf-8")).hexdigest()
    if actual_hash != public_case["prompt_sha256"]:
        raise EvalError("Public prompt hash does not match its UTF-8 content", hint="Use a validated frozen public record")
    canaries = _project_canaries(root)
    _scan_task_repo(source, canaries)
    target.mkdir(parents=True)
    repo_target = target / "repo"
    shutil.copytree(source, repo_target, symlinks=True)
    (repo_target / ".evalsys-allowed-repo").write_text("allowed\n", encoding="utf-8")
    prompt_record = {key: public_case[key] for key in _AGENT_PROMPT_KEYS}
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


def _host_probe(workspace: Path, private_canaries: set[str]) -> dict[str, bool]:
    positive = (workspace / "repo/.evalsys-allowed-repo").is_file() and (workspace / "prompt.json").is_file()
    forbidden_paths = ["benchmark/private", "Oracle", "oracle", "gold.patch", "test.patch", "hints", "计划", "资料", "artifacts", "logs", "cache", ".cache"]
    no_paths = all(not (workspace / relative).exists() for relative in forbidden_paths)
    workspace_bytes = b"\n".join(path.read_bytes() for path in workspace.rglob("*") if path.is_file())
    return {"positive": positive, "negative": no_paths and all(value.encode() not in workspace_bytes for value in private_canaries)}


def _sanitize_diagnostic(text: str, project_root: Path, destination: Path) -> str:
    value = text.replace(str(project_root), "<PROJECT_ROOT>").replace(project_root.as_posix(), "<PROJECT_ROOT>")
    value = value.replace(str(destination), "<WORKSPACE>").replace(destination.as_posix(), "<WORKSPACE>")
    value = re.sub(r"(?i)(token|password|secret|api[_-]?key)\s*[=:]\s*\S+", r"\1=[REDACTED]", value)
    value = re.sub(r"[A-Za-z]:[\\/][^\r\n\t\"]+", "<ABSOLUTE_PATH>", value)
    value = re.sub(r"/mnt/[a-z]/[^\r\n\t\"]+", "<ABSOLUTE_PATH>", value)
    return value[:1024]


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
    host = _host_probe(destination, _project_canaries(project_root))
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
        stdout = _sanitize_diagnostic(result.stdout, project_root.resolve(), destination.resolve())
        stderr = _sanitize_diagnostic(result.stderr, project_root.resolve(), destination.resolve())
        raise EvalError(f"Agent isolation executable probes failed: returncode={result.returncode}; stdout={stdout!r}; stderr={stderr!r}", hint="Ensure only the isolated workspace mount is allowed", category="infra_failed")
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
