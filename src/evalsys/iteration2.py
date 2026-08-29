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
from .baseline import FORMAL_SEED, build_formal_plan, verify_formal_plan
from .errors import EvalError
from .recovery import sha256_file


_VALID_RESULTS = {"resolved", "unresolved", "agent_no_patch", "agent_stopped", "model_error"}
_DEV_INSTANCES = (
    "django__django-11133",
    "scikit-learn__scikit-learn-14983",
    "matplotlib__matplotlib-25332",
)


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
    destination.mkdir(parents=True)
    mount_source = (path_converter or (lambda value: str(value)))(destination.resolve())
    command = [
        *docker_prefix, "run", "--rm", "--pull", "never",
        "--label", f"evalsys.run_id={run_id}", "--network", "none",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--mount", f"type=bind,src={mount_source},dst=/export",
        image, "bash", "-lc",
        f"cp -a /testbed/. /export/ && git -C /export checkout --detach {base_commit} && git -C /export reset --hard {base_commit} && git -C /export clean -ffd",
    ]
    completed = runner(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False, timeout=300,
    )
    if completed.returncode:
        raise EvalError("cannot export task repository from official image", category="infra_failed")
    head = _git(destination, "rev-parse", "HEAD")
    if head != base_commit:
        raise EvalError("exported task repository base commit mismatch", category="infra_failed")
    if _git(destination, "status", "--porcelain"):
        raise EvalError("exported task repository is dirty", category="infra_failed")
    return destination.resolve()


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


def freeze_baseline(
    project_root: Path, name: str, config: dict[str, Any], records: list[dict[str, Any]], *,
    development: dict[str, Any] | None, image_identities: dict[str, Any],
    authorized: bool, git_commit: str,
) -> Path:
    if not authorized:
        raise EvalError("freeze requires explicit user authorization", category="invalid")
    if not development or len(development.get("source_run_ids", [])) != 6:
        raise EvalError("complete development matrix evidence is required", category="invalid")
    if (
        len(image_identities) != 15
        or any(
            not isinstance(identity, dict)
            or identity.get("available") is False
            or not isinstance(identity.get("image_id"), str)
            or not identity["image_id"].startswith("sha256:")
            for identity in image_identities.values()
        )
    ):
        raise EvalError("all 15 task image identities must be resolved", category="invalid")
    baseline_root = project_root / "configs/frozen" / name
    baseline_root.mkdir(parents=True, exist_ok=False)
    test_records = [record for record in records if record.get("split") == "test"]
    plan = build_formal_plan(test_records, seed=FORMAL_SEED)
    plan_bytes = (json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    (baseline_root / "plan.json").write_bytes(plan_bytes)
    public_config = json.loads(json.dumps(config))
    system_source = project_root / "prompts/baseline/system.txt"
    protocol_source = project_root / "prompts/baseline/protocol.txt"
    system_bytes = system_source.read_bytes()
    protocol_bytes = protocol_source.read_bytes()
    tool_schema_bytes = (json.dumps(_tool_schemas(project_root, config), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    (baseline_root / "system.txt").write_bytes(system_bytes)
    (baseline_root / "protocol.txt").write_bytes(protocol_bytes)
    (baseline_root / "tool-schemas.json").write_bytes(tool_schema_bytes)
    manifest_path = project_root / "benchmark/manifests/paired-cases.jsonl"
    source_lock_path = project_root / "benchmark/source-lock.json"
    dependency_lock_path = project_root / "uv.lock"
    manifest = {
        "schema_version": "1.0", "name": name, "agent_code_commit": git_commit,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "formal_seed": FORMAL_SEED, "plan_generator": "evalsys.baseline.build_formal_plan/v1",
        "plan_sha256": _sha256_bytes(plan_bytes), "system_prompt_sha256": _sha256_bytes(system_bytes),
        "protocol_prompt_sha256": _sha256_bytes(protocol_bytes), "tool_schema_sha256": _sha256_bytes(tool_schema_bytes),
        "public_manifest_sha256": sha256_file(manifest_path), "source_lock_sha256": sha256_file(source_lock_path),
        "dependency_lock_sha256": sha256_file(dependency_lock_path),
        "provider_hard_context_limit": "unavailable",
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


def verify_frozen_baseline(root: Path) -> dict[str, Any]:
    checksum_path = root / "checksums.sha256"
    try:
        lines = checksum_path.read_text(encoding="utf-8").splitlines()
        expected = dict(line.split("  ", 1)[::-1] for line in lines)
    except (OSError, ValueError) as exc:
        raise EvalError("invalid frozen baseline checksum manifest", category="invalid") from exc
    required = {"baseline.json", "plan.json"}
    allowed = required | {"system.txt", "protocol.txt", "tool-schemas.json"}
    if not required.issubset(expected) or not set(expected).issubset(allowed):
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
    if baseline.get("plan_sha256") and baseline["plan_sha256"] != expected["plan.json"]:
        raise EvalError("frozen baseline plan checksum mismatch", category="invalid")
    return {"baseline": baseline, "plan": plan}


def classify_cell_for_resume(result: dict[str, Any] | None) -> str:
    if result is None:
        return "not_started"
    status = result.get("status")
    if status == "eval_infra_failed":
        return "retryable_infra"
    if result.get("evaluator_recorded") is True and status in _VALID_RESULTS:
        return "complete"
    return "invalid_evidence"


def _initialize_exported_repository(path: Path) -> None:
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "reqagent@example.invalid")
    _git(path, "config", "user.name", "ReqAgent")
    _git(path, "add", "-A", "--", ".")
    _git(path, "commit", "-qm", "frozen task snapshot")


def run_agent_cell(settings, case: dict[str, Any], source_row: dict[str, Any], config, *, run_root: Path) -> dict[str, Any]:
    """Run one Agent in a fresh task export, then evaluate its patch separately."""
    from reqagent.cli import _execute
    from reqagent.config import AgentConfig
    from reqagent.trace import RunStore
    from .replay import replay_case

    image = official_image_name(case["instance_id"])
    prefix = settings.docker_prefix(sys.platform)
    identity = resolve_image_identity(image, docker_prefix=prefix)
    raw = json.loads(json.dumps(config.raw))
    raw["workspace"]["container_image"] = identity["pinned"]
    effective = AgentConfig(raw, config.source)
    store = RunStore.create(run_root, kind="benchmark")
    cell_root = store.path
    export_root = Path(tempfile.mkdtemp(prefix="evalsys-task-")) / "repo"
    started = time.monotonic()
    try:
        converter = None
        if sys.platform == "win32":
            def converter(path: Path) -> str:
                completed = subprocess.run(
                    ["wsl.exe", "--", "wslpath", "-a", path.as_posix()],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
                )
                if completed.returncode:
                    raise EvalError("cannot translate task workspace path", category="infra_failed")
                return completed.stdout.strip()
        prepare_task_repository(
            docker_prefix=prefix, image=identity["pinned"], base_commit=case["base_commit"],
            destination=export_root, run_id=store.run_id, path_converter=converter,
        )
        request = AgentRunRequest.from_public_case(case, export_root)
        request.verify_repository()
        result = _execute(export_root, request.task_text, effective, store)
        source_row = dict(source_row)
        harness_revision = source_row.get("harness_revision")
        if not isinstance(harness_revision, str) or len(harness_revision) != 40:
            raise EvalError("source row is missing the frozen harness revision", category="invalid")
        source_row["docker_image"] = identity["pinned"]
        evaluation = replay_case(
            settings, case, source_row, "agent", run_id=store.run_id,
            timeout_s=effective.budgets["wall_clock_seconds"], workers=1, resume=False,
            patch_path=cell_root / "agent.patch", unit_root=cell_root / "evaluation",
        ) if result.patch.bytes else {"status": "agent_no_patch", "classification": "agent_no_patch", "tests_executed": False}
        status = evaluation["status"]
        if status in {"infra_failed", "timeout", "invalid"}:
            status = "eval_infra_failed"
        cell = {
            "run_id": store.run_id, "case_id": case["case_id"], "instance_id": case["instance_id"],
            "variant": case["prompt_variant"], "ambiguity_type": case["ambiguity_type"],
            "status": status, "evaluator_recorded": True, "stop_reason": result.stop_reason,
            "steps": result.steps, "tool_calls": result.tool_calls, "usage": result.usage,
            "wall_time_seconds": time.monotonic() - started,
            "patch": {"files": result.patch.files, "additions": result.patch.additions, "deletions": result.patch.deletions, "bytes": result.patch.bytes},
            "agent_tests": (result.submitted or {}).get("tests", []), "image": identity,
            "evaluation": evaluation,
        }
        (cell_root / "cell-result.json").write_text(json.dumps(cell, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return cell
    finally:
        shutil.rmtree(export_root.parent, ignore_errors=True)


def load_public_records(project_root: Path) -> list[dict[str, Any]]:
    path = project_root / "benchmark/manifests/paired-cases.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def run_development(settings, version: str, config, source_rows: dict[str, dict[str, Any]], *, resume: bool) -> dict[str, Any]:
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
        disposition = classify_cell_for_resume(prior)
        if disposition == "complete":
            results.append(prior)
            continue
        if prior is not None:
            raise EvalError(f"development cell cannot be selectively rerun: {cell['case_id']}", category="invalid")
        cell_root = root / "cells" / cell["case_id"]
        cell_root.mkdir(parents=True, exist_ok=False)
        result = run_agent_cell(settings, cell, source_rows[cell["instance_id"]], config, run_root=cell_root)
        (cell_root / "cell-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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
    destination.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return record


def run_formal_plan(settings, baseline_name: str, config, source_rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    frozen = verify_frozen_baseline(settings.project_root / "configs/frozen" / baseline_name)
    records = load_public_records(settings.project_root)
    verify_formal_plan(frozen["plan"], [record for record in records if record["split"] == "test"])
    by_case = {record["case_id"]: record for record in records}
    root = settings.artifact_root / "runs/iteration2/formal" / baseline_name
    root.mkdir(parents=True, exist_ok=True)
    results = []
    for planned in frozen["plan"]:
        cell = by_case[planned["case_id"]]
        result_path = root / "cells" / f"{planned['sequence']:02d}-{cell['case_id']}" / "cell-result.json"
        prior = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else None
        disposition = classify_cell_for_resume(prior)
        if disposition == "complete":
            results.append(prior)
            continue
        if prior is not None and disposition != "retryable_infra":
            raise EvalError(f"formal cell has invalid evidence: {cell['case_id']}", category="invalid")
        if prior is not None:
            raise EvalError("infrastructure reruns require an explicit superseding run implementation", category="invalid")
        cell_root = result_path.parent
        cell_root.mkdir(parents=True, exist_ok=False)
        result = run_agent_cell(settings, cell, source_rows[cell["instance_id"]], config, run_root=cell_root)
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        results.append(result)
    summary = summarize_formal_results(results)
    (root / "report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def summarize_formal_results(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if len(rows) != 24:
        raise EvalError(f"formal report requires 24 cells; found {len(rows)}", category="invalid")
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
