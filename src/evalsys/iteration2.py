from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agent_runner import AgentRunRequest
from .baseline import FORMAL_INSTANCES, FORMAL_SEED, build_formal_plan, verify_formal_plan
from .errors import EvalError
from .evidence import sanitize
from .recovery import sha256_file


_VALID_RESULTS = {"resolved", "unresolved", "agent_no_patch", "agent_stopped", "model_error"}
_DEV_INSTANCES = (
    "django__django-11133",
    "scikit-learn__scikit-learn-14983",
    "matplotlib__matplotlib-25332",
)
_DEV_CASES = {
    "django__django-11133": "D-O1",
    "scikit-learn__scikit-learn-14983": "D-S1",
    "matplotlib__matplotlib-25332": "D-R1",
}


def development_cells(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cells = []
    for instance_id in _DEV_INSTANCES:
        for variant in ("full", "fuzzy"):
            cells.append(select_public_case(records, instance_id, variant, allowed_split="dev"))
    return cells


def select_public_case(records: list[dict[str, Any]], identity: str, variant: str, *, allowed_split: str | None = None) -> dict[str, Any]:
    matches = [
        record for record in records
        if record.get("prompt_variant") == variant
        and identity in {record.get("case_id", "").removesuffix(f"-{variant}"), record.get("instance_id")}
        and (allowed_split is None or record.get("split") == allowed_split)
    ]
    if len(matches) != 1:
        raise EvalError(f"public case not found or ambiguous: {identity}/{variant}", category="invalid")
    return matches[0]


def official_image_name(instance_id: str) -> str:
    if "__" not in instance_id:
        raise ValueError("invalid SWE-bench instance id")
    owner, issue = instance_id.split("__", 1)
    repository = issue.rsplit("-", 1)[0]
    return f"swebench/sweb.eval.x86_64.{owner}_1776_{repository}-{issue.rsplit('-', 1)[1]}:latest"


def resolve_image_identity(image: str, *, docker_prefix: list[str], runner=subprocess.run) -> dict[str, Any]:
    completed = runner(
        [*docker_prefix, "image", "inspect", image], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    if completed.returncode:
        raise EvalError(f"official task image is unavailable: {image}", category="infra_failed")
    try:
        values = json.loads(completed.stdout)
        record = values[0]
        image_id = record["Id"]
        digests = sorted(record.get("RepoDigests") or [])
    except (json.JSONDecodeError, IndexError, KeyError, TypeError) as exc:
        raise EvalError(f"cannot parse task image identity: {image}", category="infra_failed") from exc
    if not isinstance(image_id, str) or not image_id.startswith("sha256:"):
        raise EvalError(f"task image has invalid image ID: {image}", category="infra_failed")
    pinned = next((digest for digest in digests if "@sha256:" in digest), None)
    return {"requested": image, "image_id": image_id, "repo_digests": digests, "pinned": pinned or image_id}


def prepare_task_repository(
    *, docker_prefix: list[str], image: str, base_commit: str, destination: Path,
    run_id: str, runner=subprocess.run, path_converter=None,
) -> Path:
    if destination.exists():
        raise FileExistsError(destination)
    staging = destination.parent.resolve()
    staging.mkdir(parents=True, exist_ok=True)
    bundle = staging / f"{run_id}.bundle"
    if bundle.exists():
        raise FileExistsError(bundle)
    mount_source = (path_converter or (lambda value: str(value)))(staging)
    command = [
        *docker_prefix, "run", "--rm", "--pull", "never",
        "--label", f"evalsys.run_id={run_id}", "--network", "none",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--mount", f"type=bind,src={mount_source},dst=/export",
        image, "bash", "-lc", f"git -C /testbed bundle create /export/{run_id}.bundle --all",
    ]
    completed = runner(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False, timeout=300,
    )
    if completed.returncode:
        raise EvalError(f"cannot export task repository from official image: {completed.stderr[-500:]}", category="infra_failed")
    clone = subprocess.run(
        ["git", "clone", "--quiet", str(bundle), str(destination)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    if clone.returncode:
        raise EvalError("cannot clone exported task bundle", category="infra_failed")
    bundle.unlink()
    repository = destination
    _git(repository, "checkout", "--detach", "--quiet", base_commit)
    _git(repository, "reset", "--hard", base_commit)
    _git(repository, "clean", "-ffd")
    head = _git(repository, "rev-parse", "HEAD")
    if head != base_commit:
        raise EvalError("exported task repository base commit mismatch", category="infra_failed")
    if _git(repository, "status", "--porcelain"):
        raise EvalError("exported task repository is dirty", category="infra_failed")
    return repository.resolve()


def build_agent_container_command(
    *, docker_prefix: list[str], image: str, workspace: Path, run_id: str,
    shell_command: str, timeout_seconds: int,
) -> list[str]:
    if "@sha256:" not in image:
        raise ValueError("task image must be pinned by digest")
    return [
        *docker_prefix, "run", "--rm", "--pull", "never",
        "--label", f"evalsys.run_id={run_id}",
        "--network", "none", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--cpus", "2",
        "--memory", "4g", "--pids-limit", "512",
        "--env", "HOME=/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1",
        "--mount", f"type=bind,src={workspace.resolve()},dst=/workspace",
        "--workdir", "/workspace", image, "timeout", str(timeout_seconds),
        "bash", "-lc", shell_command,
    ]


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    if completed.returncode:
        raise EvalError(f"git command failed: {' '.join(args)}", category="invalid")
    return completed.stdout.strip()


def verify_git_gate(project_root: Path) -> str:
    if _git(project_root, "status", "--porcelain"):
        raise EvalError("working tree is dirty", category="invalid")
    head = _git(project_root, "rev-parse", "HEAD")
    remote = _git(project_root, "rev-parse", "origin/main")
    if head != remote:
        raise EvalError("local HEAD does not equal origin/main", category="invalid")
    return head


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def config_hash(value: dict[str, Any]) -> str:
    return _sha256_bytes(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def behavior_tree_hash(project_root: Path) -> str:
    files = []
    for relative in ("src/reqagent", "src/evalsys"):
        directory = project_root / relative
        if directory.is_dir():
            files.extend(sorted(directory.rglob("*.py")))
    adapter = project_root / "scripts/official_harness_adapter.py"
    if adapter.is_file():
        files.append(adapter)
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.relative_to(project_root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def plan_generator_hash(project_root: Path) -> str:
    return sha256_file(project_root / "src/evalsys/baseline.py")


def _tool_schemas(project_root: Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    from reqagent.config import AgentConfig
    from reqagent.tools import build_registry
    from reqagent.workspace import GitWorkspace

    with tempfile.TemporaryDirectory(prefix="evalsys-schema-") as temporary:
        source = Path(temporary) / "source"
        source.mkdir()
        _git(source, "init", "-q")
        _git(source, "config", "user.email", "evalsys@example.invalid")
        _git(source, "config", "user.name", "Evalsys")
        (source / "README").write_text("schema\n", encoding="utf-8")
        _git(source, "add", "README")
        _git(source, "commit", "-qm", "schema")
        workspace = GitWorkspace.create(source)
        agent_config = AgentConfig(json.loads(json.dumps(config)), project_root / "configs/agent/live-local-proxy.json")
        registry = build_registry(workspace, agent_config.raw)
        try:
            return [definition.__dict__ for definition in registry.definitions]
        finally:
            workspace.cleanup()


def current_tool_schema_bytes(project_root: Path, config: dict[str, Any]) -> bytes:
    return (json.dumps(_tool_schemas(project_root, config), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def freeze_baseline(
    project_root: Path, name: str, config: dict[str, Any], records: list[dict[str, Any]], *,
    development: dict[str, Any] | None, image_identities: dict[str, Any],
    authorized: bool, git_commit: str,
) -> Path:
    if not authorized:
        raise EvalError("freeze requires explicit user authorization", category="invalid")
    if not development or len(development.get("source_run_ids", [])) != 6:
        raise EvalError("complete development matrix evidence is required", category="invalid")
    expected_cells = [
        (f"{_DEV_CASES[instance_id]}-{variant}", instance_id, variant)
        for instance_id in _DEV_INSTANCES
        for variant in ("full", "fuzzy")
    ]
    cells = development.get("cells")
    required_development = {
        "config_hash", "system_prompt_hash", "protocol_prompt_hash", "tool_schema_hash",
        "code_commit", "code_hash",
    }
    if not isinstance(cells, list) or len(cells) != 6 or not required_development.issubset(development):
        raise EvalError("development cell evidence or behavior binding is incomplete", category="invalid")
    actual_cells = [(cell.get("case_id"), cell.get("instance_id"), cell.get("variant")) for cell in cells]
    if actual_cells != expected_cells:
        raise EvalError("development cell identities do not match the fixed matrix", category="invalid")
    if development["source_run_ids"] != [cell.get("run_id") for cell in cells]:
        raise EvalError("development cell run IDs do not match source_run_ids", category="invalid")
    for label, field in (("test receipt", "test_receipt"), ("isolation proof", "isolation_proof")):
        receipt = development.get(field)
        if not isinstance(receipt, dict) or receipt.get("status") != "passed" or not isinstance(receipt.get("checksum"), str) or len(receipt["checksum"]) != 64:
            raise EvalError(f"valid {label} is required", category="invalid")
    frozen_config_hash = config_hash(config)
    if development.get("config_hash") != frozen_config_hash:
        raise EvalError("development config hash does not match live configuration", category="invalid")
    valid_statuses = {"resolved", "unresolved", "agent_no_patch", "agent_stopped", "model_error"}
    if any(
        cell.get("status") not in valid_statuses
        or cell.get("evaluator_recorded") is not True
        or not isinstance(cell.get("checksum"), str)
        or len(cell["checksum"]) != 64
        for cell in cells
    ):
        raise EvalError("development cell is not a complete valid Agent result", category="invalid")
    expected_images = set(_DEV_INSTANCES) | set(FORMAL_INSTANCES)
    if set(image_identities) != expected_images:
        raise EvalError("image inventory keys do not match fixed dev/formal instances", category="invalid")
    if (
        len(image_identities) != 15
        or any(
            not isinstance(identity, dict)
            or identity.get("available") is False
            or not isinstance(identity.get("image_id"), str)
            or not identity["image_id"].startswith("sha256:")
            or not any("@sha256:" in digest for digest in identity.get("repo_digests", []))
            for identity in image_identities.values()
        )
    ):
        raise EvalError("all 15 task image identities must be resolved", category="invalid")
    system_source = project_root / "prompts/baseline/system.txt"
    protocol_source = project_root / "prompts/baseline/protocol.txt"
    system_bytes = system_source.read_bytes()
    protocol_bytes = protocol_source.read_bytes()
    tool_schema_bytes = current_tool_schema_bytes(project_root, config)
    current_bindings = {
        "config_hash": frozen_config_hash,
        "system_prompt_hash": _sha256_bytes(system_bytes),
        "protocol_prompt_hash": _sha256_bytes(protocol_bytes),
        "tool_schema_hash": _sha256_bytes(tool_schema_bytes),
        "code_hash": behavior_tree_hash(project_root),
    }
    for field, digest in current_bindings.items():
        if development.get(field) != digest:
            raise EvalError(f"development {field} does not match current behavior", category="invalid")
    baseline_root = project_root / "configs/frozen" / name
    baseline_root.mkdir(parents=True, exist_ok=False)
    test_records = [record for record in records if record.get("split") == "test"]
    plan = build_formal_plan(test_records, seed=FORMAL_SEED)
    plan_bytes = (json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    (baseline_root / "plan.json").write_bytes(plan_bytes)
    public_config = json.loads(json.dumps(config))
    (baseline_root / "system.txt").write_bytes(system_bytes)
    (baseline_root / "protocol.txt").write_bytes(protocol_bytes)
    (baseline_root / "tool-schemas.json").write_bytes(tool_schema_bytes)
    manifest_path = project_root / "benchmark/manifests/paired-cases.jsonl"
    source_lock_path = project_root / "benchmark/source-lock.json"
    dependency_lock_path = project_root / "uv.lock"
    manifest = {
        "schema_version": "1.0", "name": name,
        "agent_code_commit": development["code_commit"],
        "freeze_source_commit": git_commit,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "formal_seed": FORMAL_SEED, "plan_generator": "evalsys.baseline.build_formal_plan/v1",
        "plan_generator_sha256": plan_generator_hash(project_root),
        "behavior_tree_sha256": behavior_tree_hash(project_root),
        "plan_sha256": _sha256_bytes(plan_bytes), "system_prompt_sha256": _sha256_bytes(system_bytes),
        "protocol_prompt_sha256": _sha256_bytes(protocol_bytes), "tool_schema_sha256": _sha256_bytes(tool_schema_bytes),
        "public_manifest_sha256": sha256_file(manifest_path), "source_lock_sha256": sha256_file(source_lock_path),
        "dependency_lock_sha256": sha256_file(dependency_lock_path),
        "provider_hard_context_limit": "unavailable",
        "config_sha256": frozen_config_hash,
        "config": public_config, "development": development,
        "image_identities": image_identities,
        "authorization": {
            "kind": "conditional_pre_authorization",
            "statement": "User authorized automatic continuation only after every freeze gate passes; user did not manually inspect this generated hash.",
        },
    }
    baseline_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    (baseline_root / "baseline.json").write_bytes(baseline_bytes)
    checksums = {
        "baseline.json": _sha256_bytes(baseline_bytes),
        "plan.json": _sha256_bytes(plan_bytes),
        "protocol.txt": _sha256_bytes(protocol_bytes),
        "system.txt": _sha256_bytes(system_bytes),
        "tool-schemas.json": _sha256_bytes(tool_schema_bytes),
    }
    (baseline_root / "checksums.sha256").write_text(
        "".join(f"{digest}  {path}\n" for path, digest in sorted(checksums.items())), encoding="utf-8", newline="\n",
    )
    return baseline_root


def verify_frozen_baseline(root: Path, *, project_root: Path | None = None, image_resolver=None) -> dict[str, Any]:
    checksum_path = root / "checksums.sha256"
    try:
        lines = checksum_path.read_text(encoding="utf-8").splitlines()
        expected = dict(line.split("  ", 1)[::-1] for line in lines)
    except (OSError, ValueError) as exc:
        raise EvalError("invalid frozen baseline checksum manifest", category="invalid") from exc
    required = {"baseline.json", "plan.json", "system.txt", "protocol.txt", "tool-schemas.json"}
    allowed = required
    if set(expected) != required:
        raise EvalError("invalid frozen baseline checksum paths", category="invalid")
    for name, digest in expected.items():
        path = root / name
        if not path.is_file() or _sha256_bytes(path.read_bytes()) != digest:
            raise EvalError(f"frozen baseline checksum mismatch: {name}", category="invalid")
    try:
        baseline = json.loads((root / "baseline.json").read_text(encoding="utf-8"))
        plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvalError("invalid frozen baseline JSON", category="invalid") from exc
    bindings = {
        "plan_sha256": "plan.json",
        "system_prompt_sha256": "system.txt",
        "protocol_prompt_sha256": "protocol.txt",
        "tool_schema_sha256": "tool-schemas.json",
    }
    for field, name in bindings.items():
        if field in baseline and baseline[field] != expected[name]:
            raise EvalError(f"frozen baseline {name} checksum mismatch", category="invalid")
    if project_root is not None:
        if baseline.get("config_sha256") != config_hash(baseline.get("config", {})):
            raise EvalError("frozen live config hash mismatch", category="invalid")
        try:
            schema_bytes = current_tool_schema_bytes(project_root, baseline.get("config", {}))
        except (OSError, ValueError) as exc:
            raise EvalError("current tool schema cannot be generated", category="invalid") from exc
        if _sha256_bytes(schema_bytes) != baseline.get("tool_schema_sha256"):
            raise EvalError("current tool schema does not match frozen baseline", category="invalid")
        current = {
            "public_manifest_sha256": sha256_file(project_root / "benchmark/manifests/paired-cases.jsonl"),
            "source_lock_sha256": sha256_file(project_root / "benchmark/source-lock.json"),
            "dependency_lock_sha256": sha256_file(project_root / "uv.lock"),
            "plan_generator_sha256": plan_generator_hash(project_root),
            "behavior_tree_sha256": behavior_tree_hash(project_root),
            "system_prompt_sha256": sha256_file(project_root / "prompts/baseline/system.txt"),
            "protocol_prompt_sha256": sha256_file(project_root / "prompts/baseline/protocol.txt"),
        }
        for field, digest in current.items():
            if baseline.get(field) != digest:
                raise EvalError(f"current {field} does not match frozen baseline", category="invalid")
    if image_resolver is not None:
        for instance_id, frozen_identity in baseline.get("image_identities", {}).items():
            current = image_resolver(instance_id)
            if current.get("image_id") != frozen_identity.get("image_id") or set(current.get("repo_digests", [])) != set(frozen_identity.get("repo_digests", [])):
                raise EvalError(f"frozen image identity drift: {instance_id}", category="infra_failed")
    return {"baseline": baseline, "plan": plan}


def classify_cell_for_resume(result: dict[str, Any] | None) -> str:
    if result is None:
        return "not_started"
    status = result.get("status")
    if status == "eval_infra_failed" and result.get("evaluator_recorded") is True:
        return "retryable_infra"
    if result.get("evaluator_recorded") is True and status in _VALID_RESULTS:
        return "complete"
    return "invalid_evidence"


def cell_resume_action(result: dict[str, Any] | None, *, resume: bool) -> str:
    disposition = classify_cell_for_resume(result)
    if disposition == "not_started":
        return "start"
    if disposition == "complete":
        if not resume:
            raise EvalError("completed cells may only be reused with explicit --resume", category="invalid")
        return "reuse"
    if disposition == "retryable_infra":
        if not resume:
            raise EvalError("evaluator infrastructure retry requires explicit --resume", category="invalid")
        return "retry_infra"
    raise EvalError("cell evidence or checkpoint is invalid; automatic resampling is forbidden", category="invalid")


def start_infra_retry(recorder, prior: dict[str, Any], config: dict[str, Any], command: list[str]):
    if classify_cell_for_resume(prior) != "retryable_infra":
        raise EvalError("only explicit evaluator infrastructure failures may be superseded", category="invalid")
    return recorder.start("formal_cell", config, command, supersedes=[prior["run_id"]])


def ensure_experiment_manifest(root: Path, manifest: dict[str, Any]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "experiment-manifest.json"
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EvalError("formal experiment manifest is invalid", category="invalid") from exc
        stable_existing = {key: value for key, value in existing.items() if key != "cell_runs"}
        stable_requested = {key: value for key, value in manifest.items() if key != "cell_runs"}
        if stable_existing != stable_requested:
            raise EvalError("formal experiment manifest mismatch", category="invalid")
    else:
        manifest = {**manifest, "cell_runs": manifest.get("cell_runs", {})}
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def record_experiment_cell(root: Path, case_id: str, run_id: str, state: str) -> None:
    if state not in {"pending", "complete", "eval_infra_failed"}:
        raise EvalError("invalid experiment cell state", category="invalid")
    path = root / "experiment-manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvalError("formal experiment manifest is invalid", category="invalid") from exc
    runs = manifest.setdefault("cell_runs", {})
    existing = runs.get(case_id)
    if existing and existing.get("run_id") != run_id and existing.get("state") != "eval_infra_failed":
        raise EvalError("experiment cell run_id cannot be overwritten", category="invalid")
    entry = {"run_id": run_id, "state": state}
    if existing and existing.get("run_id") != run_id:
        entry["supersedes"] = [existing["run_id"]]
    runs[case_id] = entry
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def verify_cell_evidence(raw_dir: Path, *, expected_run_id: str) -> dict[str, Any]:
    if not (raw_dir / "COMPLETE").is_file():
        raise EvalError("cell evidence is missing COMPLETE", category="invalid")
    result_path = raw_dir / "cell-result.json"
    checksum_path = raw_dir / "checksums.sha256"
    if not result_path.is_file() or not checksum_path.is_file():
        raise EvalError("cell evidence is incomplete", category="invalid")
    entries: dict[str, str] = {}
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        parts = line.split("  ", 1)
        if len(parts) == 2:
            entries[parts[1]] = parts[0]
    if entries.get("cell-result.json") != sha256_file(result_path):
        raise EvalError("cell-result.json checksum mismatch", category="invalid")
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvalError("cell-result.json is invalid", category="invalid") from exc
    if result.get("run_id") != expected_run_id:
        raise EvalError("cell evidence run_id mismatch", category="invalid")
    return result


def extract_actual_model(events_path: Path) -> str:
    if not events_path.is_file():
        return "unavailable"
    for line in reversed(events_path.read_text(encoding="utf-8").splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        model = event.get("response", {}).get("actual_model") if event.get("kind") == "model_response" else None
        if isinstance(model, str) and model:
            return model
    return "unavailable"


def _initialize_exported_repository(path: Path) -> None:
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "reqagent@example.invalid")
    _git(path, "config", "user.name", "ReqAgent")
    _git(path, "add", "-A", "--", ".")
    _git(path, "commit", "-qm", "frozen task snapshot")


def run_agent_cell(
    settings, case: dict[str, Any], source_row: dict[str, Any], config, *,
    image_identity: dict[str, Any], run_type: str, supersedes: list[str] | None = None,
    resume_run_id: str | None = None, on_started=None,
) -> dict[str, Any]:
    """Run one Agent in a fresh task export, then evaluate its patch separately."""
    from reqagent.cli import _execute, _resume_execute
    from reqagent.config import AgentConfig
    from reqagent.loop import AgentInterrupted
    from reqagent.trace import RunStore
    from .evidence import EvidenceRecorder
    from .replay import replay_case

    prefix = settings.docker_prefix(sys.platform)
    image = next((digest for digest in image_identity.get("repo_digests", []) if "@sha256:" in digest), None)
    if not image:
        raise EvalError("frozen task image has no RepoDigest", category="invalid")
    identity = resolve_image_identity(image, docker_prefix=prefix)
    if identity["image_id"] != image_identity.get("image_id") or set(identity["repo_digests"]) != set(image_identity.get("repo_digests", [])):
        raise EvalError(f"frozen image identity drift: {case['instance_id']}", category="infra_failed")
    raw = json.loads(json.dumps(config.raw))
    raw["workspace"]["container_image"] = image
    effective = AgentConfig(raw, config.source)
    recorder = EvidenceRecorder(settings.project_root, iteration=2, raw_root=settings.artifact_root / "runs/iteration2")
    evidence_config = {"case_id": case["case_id"], "config_hash": effective.canonical_hash()}
    command = ["evalsys", run_type]
    if resume_run_id:
        evidence = recorder.resume_pending(resume_run_id, run_type, evidence_config, command)
    else:
        evidence = recorder.start(run_type, evidence_config, command, supersedes=supersedes)
    store = RunStore.open(evidence.raw_dir)
    if on_started is not None:
        on_started(store.run_id)
    export_root = evidence.raw_dir / "source" / "repo"
    agent_clone = evidence.raw_dir / "workspace" / "repo"
    started = time.monotonic()
    completed = False
    try:
        if resume_run_id:
            result = _resume_execute(effective, store, finalize=False)
        else:
            converter = None
            if sys.platform == "win32":
                def converter(path: Path) -> str:
                    converted = subprocess.run(
                        ["wsl.exe", "--", "wslpath", "-a", path.as_posix()],
                        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
                    )
                    if converted.returncode:
                        raise EvalError("cannot translate task workspace path", category="infra_failed")
                    return converted.stdout.strip()
            prepare_task_repository(
                docker_prefix=prefix, image=image, base_commit=case["base_commit"],
                destination=export_root, run_id=store.run_id, path_converter=converter,
            )
            request = AgentRunRequest.from_public_case(case, export_root)
            request.verify_repository()
            result = _execute(export_root, request.task_text, effective, store, destination=agent_clone, finalize=False)
        source_row = dict(source_row)
        harness_revision = source_row.get("harness_revision")
        if not isinstance(harness_revision, str) or len(harness_revision) != 40:
            raise EvalError("source row is missing the frozen harness revision", category="invalid")
        source_row["docker_image"] = image
        evaluation = replay_case(
            settings, case, source_row, "agent", run_id=store.run_id,
            timeout_s=effective.budgets["wall_clock_seconds"], workers=1, resume=False,
            patch_path=store.path / "agent.patch", unit_root=store.path / "evaluation",
        ) if result.patch.bytes else {"status": "agent_no_patch", "classification": "agent_no_patch", "tests_executed": False}
        status = evaluation["status"]
        if status in {"infra_failed", "invalid"}:
            status = "eval_infra_failed"
        cell = {
            "run_id": store.run_id, "case_id": case["case_id"], "instance_id": case["instance_id"],
            "variant": case["prompt_variant"], "ambiguity_type": case["ambiguity_type"],
            "status": status, "evaluator_recorded": True, "stop_reason": result.stop_reason,
            "steps": result.steps, "tool_calls": result.tool_calls, "usage": result.usage,
            "wall_time_seconds": time.monotonic() - started,
            "patch": {"files": result.patch.files, "additions": result.patch.additions, "deletions": result.patch.deletions, "bytes": result.patch.bytes},
            "agent_tests": (result.submitted or {}).get("tests", []), "image": identity,
            "actual_model": extract_actual_model(store.path / "events.jsonl"),
            "evaluation": evaluation,
        }
        (store.path / "cell-result.json").write_text(json.dumps(cell, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        evidence.finish({"status": status, "classification": evaluation["classification"]})
        completed = True
        return cell
    except AgentInterrupted:
        raise
    except Exception as exc:
        evidence.fail({"status": "failed", "classification": "cell_interrupted", "reason": str(exc)})
        raise
    finally:
        if completed:
            shutil.rmtree(evidence.raw_dir / "source", ignore_errors=True)
            shutil.rmtree(evidence.raw_dir / "workspace", ignore_errors=True)


def load_public_records(project_root: Path) -> list[dict[str, Any]]:
    path = project_root / "benchmark/manifests/paired-cases.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def run_development(settings, version: str, config, source_rows: dict[str, dict[str, Any]], image_identities: dict[str, Any], *, resume: bool) -> dict[str, Any]:
    if not __import__("re").fullmatch(r"v\d{3}", version):
        raise EvalError("development version must match vNNN", category="invalid")
    records = load_public_records(settings.project_root)
    root = settings.artifact_root / "runs/iteration2/dev" / version
    root.mkdir(parents=True, exist_ok=True)
    plan_path = root / "plan.json"
    cells = development_cells(records)
    identities = [{"case_id": cell["case_id"], "instance_id": cell["instance_id"], "variant": cell["prompt_variant"]} for cell in cells]
    if plan_path.exists():
        if json.loads(plan_path.read_text(encoding="utf-8")) != identities:
            raise EvalError("development resume plan mismatch", category="invalid")
    else:
        plan_path.write_text(json.dumps(identities, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    results = []
    for cell in cells:
        result_path = root / "cells" / cell["case_id"] / "cell-result.json"
        prior = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else None
        if prior is not None and classify_cell_for_resume(prior) == "complete":
            verify_cell_evidence(settings.artifact_root / "runs/iteration2" / prior["run_id"], expected_run_id=prior["run_id"])
        action = cell_resume_action(prior, resume=resume)
        if action == "reuse":
            results.append(prior)
            continue
        if action == "retry_infra":
            raise EvalError("development infrastructure failure invalidates the complete vNNN matrix", category="invalid")
        result = run_agent_cell(
            settings, cell, source_rows[cell["instance_id"]], config,
            image_identity=image_identities[cell["instance_id"]], run_type="dev_cell",
        )
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(sanitize(result, project_root=settings.project_root), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        results.append(result)
    record = {
        "version": version, "parent": None, "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "code_commit": _git(settings.project_root, "rev-parse", "HEAD"), "config_hash": config.canonical_hash(),
        "source_run_ids": [row["run_id"] for row in results], "observed_issue": "",
        "hypothesis": "Initial frozen development matrix", "exact_change": "Initial candidate",
        "expected_effect": "Establish benchmark baseline", "rollback_risk": "none",
        "validation_plan": "Complete all six full/fuzzy cells",
        "before": {"resolved": None, "stop_reasons": {}, "median_steps": None, "total_tokens": None, "wall_time_seconds": None},
        "after": {"resolved": sum(row["status"] == "resolved" for row in results), "stop_reasons": {}, "median_steps": None, "total_tokens": sum(sum(row["usage"].values()) for row in results), "wall_time_seconds": sum(row["wall_time_seconds"] for row in results)},
        "decision": "accepted", "rationale": "All six cells produced Agent/evaluator records.", "successor": None,
        "cells": results,
    }
    destination = settings.project_root / "audit/iteration2/development" / f"{version}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(sanitize(record, project_root=settings.project_root), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return record


def run_formal_plan(settings, baseline_name: str, config, source_rows: dict[str, dict[str, Any]], *, resume: bool) -> dict[str, Any]:
    prefix = settings.docker_prefix(sys.platform)
    baseline_root = settings.project_root / "configs/frozen" / baseline_name
    initial = verify_frozen_baseline(baseline_root)
    frozen = verify_frozen_baseline(
        baseline_root,
        project_root=settings.project_root,
        image_resolver=lambda instance_id: resolve_image_identity(
            next(digest for digest in initial["baseline"]["image_identities"][instance_id]["repo_digests"] if "@sha256:" in digest),
            docker_prefix=prefix,
        ),
    )
    records = load_public_records(settings.project_root)
    verify_formal_plan(frozen["plan"], [record for record in records if record["split"] == "test"])
    by_case = {record["case_id"]: record for record in records}
    root = settings.artifact_root / "runs/iteration2/formal" / baseline_name
    manifest_path = ensure_experiment_manifest(root, {
        "schema_version": "1.0", "baseline": baseline_name,
        "plan_sha256": frozen["baseline"]["plan_sha256"], "cells": 24,
        "agent_code_commit": frozen["baseline"]["agent_code_commit"],
        "behavior_tree_sha256": frozen["baseline"]["behavior_tree_sha256"],
    })
    results = []
    for planned in frozen["plan"]:
        cell = by_case[planned["case_id"]]
        result_path = root / "cells" / f"{planned['sequence']:02d}-{cell['case_id']}" / "cell-result.json"
        prior = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else None
        experiment = json.loads(manifest_path.read_text(encoding="utf-8"))
        tracked = experiment.get("cell_runs", {}).get(cell["case_id"])
        resume_run_id = None
        if tracked and tracked.get("state") == "pending":
            if prior is not None or not resume:
                raise EvalError("pending formal cell requires explicit --resume and no completed result", category="invalid")
            resume_run_id = tracked.get("run_id")
            if not isinstance(resume_run_id, str):
                raise EvalError("pending formal cell has invalid run_id", category="invalid")
        elif tracked and tracked.get("state") == "complete" and prior is None:
            raise EvalError("formal experiment manifest references missing completed evidence", category="invalid")
        if prior is not None and classify_cell_for_resume(prior) == "complete":
            verify_cell_evidence(settings.artifact_root / "runs/iteration2" / prior["run_id"], expected_run_id=prior["run_id"])
        action = "resume_pending" if resume_run_id else cell_resume_action(prior, resume=resume)
        if action == "reuse":
            results.append(prior)
            continue
        supersedes = [prior["run_id"]] if action == "retry_infra" else []
        result = run_agent_cell(
            settings, cell, source_rows[cell["instance_id"]], config,
            image_identity=frozen["baseline"]["image_identities"][cell["instance_id"]],
            run_type="formal_cell", supersedes=supersedes, resume_run_id=resume_run_id,
            on_started=lambda run_id, case_id=cell["case_id"]: record_experiment_cell(root, case_id, run_id, "pending"),
        )
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(sanitize(result, project_root=settings.project_root), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        state = "eval_infra_failed" if result["status"] == "eval_infra_failed" else "complete"
        record_experiment_cell(root, cell["case_id"], result["run_id"], state)
        results.append(result)
    summary = summarize_formal_results(results)
    (root / "report.json").write_text(json.dumps(sanitize(summary, project_root=settings.project_root), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def summarize_formal_results(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if len(rows) != 24:
        raise EvalError(f"formal report requires 24 cells; found {len(rows)}", category="invalid")
    if any(row.get("status") not in _VALID_RESULTS or row.get("evaluator_recorded") is not True for row in rows):
        raise EvalError("formal report contains incomplete or invalid cell evidence", category="invalid")
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    categories: dict[str, dict[str, dict[str, int]]] = {}
    for row in rows:
        variants = grouped.setdefault(row["instance_id"], {})
        if row["variant"] in variants:
            raise EvalError("formal report contains a duplicate paired cell", category="invalid")
        variants[row["variant"]] = row
        category = categories.setdefault(row["ambiguity_type"], {
            "E1": {"count": 0, "total": 0}, "E2": {"count": 0, "total": 0},
        })
        experiment = "E1" if row["variant"] == "full" else "E2"
        category[experiment]["total"] += 1
        category[experiment]["count"] += int(row["status"] == "resolved")
    if len(grouped) != 12 or any(set(variants) != {"full", "fuzzy"} for variants in grouped.values()):
        raise EvalError("formal report requires 12 complete full/fuzzy pairs", category="invalid")
    e1 = [row for row in rows if row["variant"] == "full"]
    e2 = [row for row in rows if row["variant"] == "fuzzy"]
    paired = {"both": 0, "full_only": 0, "fuzzy_only": 0, "neither": 0}
    for variants in grouped.values():
        full = variants.get("full", {}).get("status") == "resolved"
        fuzzy = variants.get("fuzzy", {}).get("status") == "resolved"
        key = "both" if full and fuzzy else "full_only" if full else "fuzzy_only" if fuzzy else "neither"
        paired[key] += 1
    e1_count = sum(row["status"] == "resolved" for row in e1)
    e2_count = sum(row["status"] == "resolved" for row in e2)
    return {
        "E1_resolved": {"count": e1_count, "total": len(e1)},
        "E2_resolved": {"count": e2_count, "total": len(e2)},
        "absolute_drop": e1_count - e2_count,
        "categories": categories,
        "paired_outcomes": paired,
        "cells": rows,
        "cost": "unavailable",
    }
