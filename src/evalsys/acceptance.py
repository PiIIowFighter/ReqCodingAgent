from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

ACCEPTANCE_KEYS = (
    "schema_pairs_12_3", "distribution_4_4_4_1_1_1", "official_hashes_15", "pair_official_fields_equal",
    "noop_15_passed", "gold_15_passed", "structured_failures_no_replacement", "executable_isolation",
    "validate_all_resumable", "machine_and_markdown_reports", "unit_tests_passed", "readme_compliant",
    "git_no_secrets_caches_datasets_logs", "materials_untracked_plan_isolated", "origin_exact", "sanitized_audit",
)
_REQUIRED_ORIGIN = "git@github.com:PiIIowFighter/ReqCodingAgent.git"


def _git(root: Path, *args: str) -> tuple[int, str]:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    return result.returncode, result.stdout.strip()


def evaluate_acceptance(project_root: Path, validation_report: dict[str, Any], *, unit_tests_passed: bool) -> dict[str, Any]:
    root = project_root.resolve()
    rows = validation_report.get("results", [])
    noop = [row for row in rows if row.get("mode") == "noop"]
    gold = [row for row in rows if row.get("mode") == "gold"]
    readme = root / "README.txt"
    readme_text = readme.read_text(encoding="utf-8") if readme.is_file() else ""
    readme_ok = len(readme_text) <= 1000 and all(term in readme_text for term in ("github.com/PiIIowFighter/ReqCodingAgent", "validate-all", "WSL2", "Docker"))
    _, tracked = _git(root, "ls-files")
    tracked_paths = tracked.splitlines()
    _, origin = _git(root, "remote", "get-url", "origin")
    suspicious = re.compile(r"(?i)(^|/)(artifacts|\.env|cache)(/|$)|\.(log|parquet)$")
    git_clean = not any(suspicious.search(path) for path in tracked_paths)
    audit = root / "audit/iteration1"
    audit_ok = all((audit / name).is_file() for name in ("validation-summary.json", "noop-gold-matrix.md", "run-manifest.json", "test-summary.txt", "isolation-proof.json"))
    criteria = {
        "schema_pairs_12_3": False, "distribution_4_4_4_1_1_1": False, "official_hashes_15": False,
        "pair_official_fields_equal": False,
        "noop_15_passed": len(noop) == 15 and all(row.get("status") == "passed" for row in noop),
        "gold_15_passed": len(gold) == 15 and all(row.get("status") == "passed" for row in gold),
        "structured_failures_no_replacement": len({(row.get("instance_id"), row.get("mode")) for row in rows}) == len(rows),
        "executable_isolation": (audit / "isolation-proof.json").is_file(),
        "validate_all_resumable": bool(validation_report.get("run_id")),
        "machine_and_markdown_reports": bool(validation_report.get("results")),
        "unit_tests_passed": unit_tests_passed, "readme_compliant": readme_ok,
        "git_no_secrets_caches_datasets_logs": git_clean,
        "materials_untracked_plan_isolated": not any(path == "资料" or path.startswith("资料/") for path in tracked_paths),
        "origin_exact": origin == _REQUIRED_ORIGIN, "sanitized_audit": audit_ok,
    }
    data_checks = validation_report.get("data_checks", {})
    for key in ("schema_pairs_12_3", "distribution_4_4_4_1_1_1", "official_hashes_15", "pair_official_fields_equal"):
        criteria[key] = data_checks.get(key) is True
    complete = validation_report.get("counts") == {"expected": 30, "passed": 30, "failed": 0} and all(criteria.values())
    return {"schema_version": "1.0", "criteria": criteria, "iteration_completion": complete}
