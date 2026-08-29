from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .frozen_cases import CASE_IDS
from .schema import validate_json

ACCEPTANCE_KEYS = (
    "schema_pairs_12_3", "distribution_4_4_4_1_1_1", "official_hashes_15", "pair_official_fields_equal",
    "pair_language_consistent", "django_noop_passed", "django_gold_passed", "executable_isolation",
    "smoke_report_valid", "unit_tests_passed", "readme_compliant", "git_no_secrets_caches_datasets_logs",
    "materials_untracked_plan_isolated", "origin_exact", "sanitized_audit",
)
_REQUIRED_ORIGIN = "git@github.com:PiIIowFighter/ReqCodingAgent.git"
_SECRET = re.compile(r"(?i)(?<![A-Za-z0-9_])(api[_-]?key|token|password|secret)\s*[=:]\s*(['\"]?)(?!\[?redacted\]?|secret\b|test\b|placeholder\b)[^\s'\"]{12,}\2|ssh-(rsa|ed25519)\s+[A-Za-z0-9+/]{64,}")


def _contains_secret(text: str) -> bool:
    return bool(_SECRET.search(text))


def _tracked_blob(root: Path, relative: str) -> bytes:
    result = subprocess.run(["git", "show", f":{relative}"], cwd=root, capture_output=True, check=False)
    return result.stdout if result.returncode == 0 else b""


def _tracked_text(root: Path, relative: str) -> str:
    return _tracked_blob(root, relative).decode("utf-8")


def _is_valid_isolation_proof(proof: dict[str, Any]) -> bool:
    positive = {"task_repo", "single_public_prompt"}
    negative = {"benchmark_private", "oracle", "gold_patch", "test_patch", "hints", "plan", "materials", "evaluator_logs", "evaluator_cache", "private_canaries"}
    sha256 = re.compile(r"[0-9a-f]{64}")
    return (
        proof.get("status") == "passed"
        and proof.get("sanitized") is True
        and proof.get("host_probe") == {"positive": True, "negative": True}
        and proof.get("container_probe") == {"positive": True, "negative": True}
        and proof.get("container_mount_count") == 1
        and proof.get("project_root_mounted") is False
        and proof.get("forbidden_mounts") == []
        and proof.get("forbidden_allowlist_entries") == []
        and set(proof.get("positive_probe_categories", [])) == positive
        and set(proof.get("negative_probe_categories", [])) == negative
        and sha256.fullmatch(str(proof.get("prompt_file_sha256", ""))) is not None
        and sha256.fullmatch(str(proof.get("workspace_manifest_sha256", ""))) is not None
    )


def _git(root: Path, *args: str) -> tuple[int, str]:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    return result.returncode, result.stdout.strip()


def _smoke_rows_bound(root: Path, rows: list[dict[str, Any]]) -> bool:
    from .evidence import select_current_runs
    from .recovery import load_reusable_run, sha256_file
    try:
        index = json.loads((root / "audit/iteration1/index.json").read_text(encoding="utf-8"))
        replay = [entry for entry in index["runs"] if entry.get("run_type") in {"replay_noop", "replay_gold"}]
        leaves = {entry["run_id"]: entry for entry in select_current_runs(replay)}
        for row in rows:
            run_id, mode = row["run_id"], row["mode"]
            entry = leaves.get(run_id)
            expected_raw = f"artifacts/runs/iteration1/{run_id}"
            if not entry or entry.get("status") != "passed" or entry.get("raw_path") != expected_raw:
                return False
            case = root / expected_raw / "cases/D-O1-full" / mode
            marker = json.loads((case / "COMPLETE").read_text(encoding="utf-8"))
            result = load_reusable_run(case, marker["input_fingerprint"])
            if result is None or result["instance_id"] != "django__django-11133" or result["mode"] != mode:
                return False
            if sha256_file(case / "result.json") != row["raw_result_sha256"]:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False


def evaluate_acceptance(project_root: Path, validation_report: dict[str, Any], *, unit_tests_passed: bool) -> dict[str, Any]:
    root = project_root.resolve()
    rows = validation_report.get("results", [])
    expected_cells = {("django__django-11133", mode) for mode in ("noop", "gold")}
    actual_cells = {(row.get("instance_id"), row.get("mode")) for row in rows}
    rows_valid = len(rows) == 2 and actual_cells == expected_cells and _smoke_rows_bound(root, rows)
    noop = [row for row in rows if row.get("mode") == "noop"]
    gold = [row for row in rows if row.get("mode") == "gold"]
    readme = root / "README.txt"
    readme_text = readme.read_text(encoding="utf-8") if readme.is_file() else ""
    readme_ok = len(readme_text) <= 1000 and all(term in readme_text for term in ("github.com/PiIIowFighter/ReqCodingAgent", "validate-all", "WSL2", "Docker"))
    _, tracked = _git(root, "ls-files")
    tracked_paths = tracked.splitlines()
    _, origin = _git(root, "remote", "get-url", "origin")
    _, head = _git(root, "rev-parse", "HEAD")
    _, remote_line = _git(root, "ls-remote", "origin", "refs/heads/main")
    remote_head = remote_line.split()[0] if remote_line else ""
    _, protected_changes = _git(root, "status", "--porcelain=v1", "--", "计划", "资料")
    suspicious = re.compile(r"(?i)(^|/)(artifacts|\.env|cache)(/|$)|\.(log|parquet)$")
    unsafe_content = False
    oversized = False
    for relative in tracked_paths:
        blob = _tracked_blob(root, relative)
        oversized = oversized or len(blob) > 1_000_000
        if len(blob) <= 1_000_000:
            try:
                unsafe_content = unsafe_content or _contains_secret(blob.decode("utf-8"))
            except UnicodeDecodeError:
                pass
    git_clean = not any(suspicious.search(path) for path in tracked_paths) and not unsafe_content and not oversized
    audit = root / "audit/iteration1"
    smoke_artifacts_ok = False
    proof_ok = False
    try:
        published_smoke = json.loads((audit / "smoke-summary.json").read_text(encoding="utf-8"))
        matrix_path = audit / "smoke-matrix.md"
        from .recovery import sha256_file
        smoke_artifacts_ok = published_smoke == validation_report and published_smoke.get("matrix_sha256") == sha256_file(matrix_path)
        proof = json.loads((audit / "isolation-proof.json").read_text(encoding="utf-8"))
        proof_ok = _is_valid_isolation_proof(proof)
    except (OSError, json.JSONDecodeError):
        pass
    from .evidence import scan_audit_local_paths
    audit_ok = smoke_artifacts_ok and not scan_audit_local_paths(root)
    receipt = validation_report.get("validation_receipt")
    receipt_ok = False
    if isinstance(receipt, dict):
        try:
            validate_json(receipt, "validation-receipt")
            from .recovery import sha256_file
            lock = json.loads((root / "benchmark/source-lock.json").read_text(encoding="utf-8"))
            expected_heads = {name: item["revision"] for name, item in lock["sources"].items()}
            expected_inputs = {
                "public_manifest_sha256": sha256_file(root / "benchmark/manifests/paired-cases.jsonl"),
                "oracle_manifest_sha256": sha256_file(root / "benchmark/private/oracles.jsonl"),
            }
            receipt_ok = receipt["source_heads"] == expected_heads and receipt["inputs"] == expected_inputs
        except Exception:
            pass
    criteria = {
        "schema_pairs_12_3": False, "distribution_4_4_4_1_1_1": False, "official_hashes_15": False,
        "pair_official_fields_equal": False, "pair_language_consistent": False,
        "django_noop_passed": rows_valid and len(noop) == 1 and noop[0].get("run_status") == "passed" and noop[0].get("tests_executed") is True and noop[0].get("validity") == "active",
        "django_gold_passed": rows_valid and len(gold) == 1 and gold[0].get("run_status") == "passed" and gold[0].get("tests_executed") is True and gold[0].get("validity") == "active",
        "executable_isolation": proof_ok,
        "smoke_report_valid": rows_valid and validation_report.get("status") == "passed" and smoke_artifacts_ok,
        "unit_tests_passed": unit_tests_passed, "readme_compliant": readme_ok,
        "git_no_secrets_caches_datasets_logs": git_clean,
        "materials_untracked_plan_isolated": not protected_changes and not any(path == "资料" or path.startswith("资料/") for path in tracked_paths),
        "origin_exact": origin == _REQUIRED_ORIGIN and bool(head) and head == remote_head, "sanitized_audit": audit_ok,
    }
    checks = receipt.get("checks", {}) if receipt_ok else {}
    for key in ("schema_pairs_12_3", "distribution_4_4_4_1_1_1", "official_hashes_15", "pair_official_fields_equal", "pair_language_consistent"):
        criteria[key] = checks.get(key) is True
    complete = all(criteria.values())
    return {"schema_version": "1.0", "criteria": criteria, "iteration_completion": complete}
