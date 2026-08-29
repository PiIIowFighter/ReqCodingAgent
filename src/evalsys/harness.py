from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .domain import TEST_OUTCOMES


@dataclass(frozen=True)
class HarnessInvocation:
    harness_checkout: Path
    adapter_path: Path
    dataset_path: Path
    predictions_path: Path
    report_dir: Path
    run_id: str
    instance_id: str
    mode: str
    timeout_s: int
    max_workers: int = 1
    case_id: str | None = None


def build_harness_command(invocation: HarnessInvocation, *, platform_name: str = sys.platform, python_executable: str | None = None, wsl_python: str = "python3.11", path_converter: Callable[[Path], str] = str) -> list[str]:
    if invocation.mode not in {"noop", "gold", "agent"}:
        raise ValueError(f"unsupported replay mode: {invocation.mode}")
    if invocation.max_workers < 1:
        raise ValueError("max_workers must be positive")
    windows = platform_name == "win32"
    prefix = ["wsl.exe", "--", wsl_python] if windows else [python_executable or "python3.11"]
    convert = path_converter if windows else str
    predictions = "gold" if invocation.mode == "gold" else convert(invocation.predictions_path)
    pgid_file = f"/tmp/evalsys-{invocation.run_id}-{invocation.instance_id}.pgid"
    command = prefix + [convert(invocation.adapter_path), "--pgid-file", pgid_file, "--harness-checkout", convert(invocation.harness_checkout), "--dataset", convert(invocation.dataset_path), "--predictions", predictions, "--report-dir", convert(invocation.report_dir), "--run-id", invocation.run_id, "--instance-id", invocation.instance_id, "--timeout", str(invocation.timeout_s), "--max-workers", str(invocation.max_workers), "--label", f"evalsys.run_id={invocation.run_id}"]
    if invocation.case_id:
        command += ["--label", f"evalsys.case_id={invocation.case_id}"]
    if invocation.mode == "noop":
        command.append("--skip-patch")
    return command


def extract_test_outcomes(raw: dict, expected_f2p: list[str], expected_p2p: list[str], *, tests_executed: bool) -> dict[str, str]:
    if not tests_executed:
        raise ValueError("missing actual test execution marker")
    direct = raw.get("outcomes")
    if direct is not None:
        if not isinstance(direct, dict) or any(not isinstance(name, str) or status not in TEST_OUTCOMES for name, status in direct.items()):
            raise ValueError("unknown or unparseable raw test status")
        expected_list = expected_f2p + expected_p2p
        if len(expected_list) != len(set(expected_list)):
            raise ValueError("one raw key cannot map multiple expected tests")
        canonical: dict[str, str] = {}
        used: set[str] = set()
        for expected in expected_list:
            if expected in direct:
                matches = [expected]
            elif expected.count("[") > expected.count("]"):
                matches = [name for name in direct if name.startswith(expected)]
            else:
                matches = []
            if not matches:
                raise ValueError(f"missing expected test outcome: {expected}")
            passing = {direct[name] in {"PASSED", "XFAIL"} for name in matches}
            if len(passing) != 1:
                raise ValueError(f"ambiguous truncated test outcome: {expected}")
            chosen = matches[0]
            if chosen in used:
                raise ValueError("one raw key mapped to multiple expected tests")
            used.add(chosen)
            canonical[expected] = direct[chosen]
        return canonical
    tests_status = raw.get("tests_status")
    if not isinstance(tests_status, dict):
        raise ValueError("unparseable tests_status")
    expected_groups = {"FAIL_TO_PASS": expected_f2p, "PASS_TO_PASS": expected_p2p}
    outcomes: dict[str, str] = {}
    for group, expected in expected_groups.items():
        value = tests_status.get(group)
        if not isinstance(value, dict):
            raise ValueError(f"unparseable {group}")
        unknown = set(value) - {"success", "failure", "error", "skipped", "xfail"}
        if unknown:
            raise ValueError(f"unknown outcome buckets in {group}: {sorted(unknown)}")
        for bucket, status in {"success": "PASSED", "failure": "FAILED", "error": "ERROR", "skipped": "SKIPPED", "xfail": "XFAIL"}.items():
            tests = value.get(bucket, [])
            if not isinstance(tests, list) or any(not isinstance(test, str) for test in tests):
                raise ValueError(f"unparseable {group}.{bucket}")
            for test in tests:
                if test in outcomes:
                    raise ValueError(f"duplicate test outcome: {test}")
                outcomes[test] = status
        missing = set(expected) - outcomes.keys()
        if missing:
            raise ValueError(f"missing expected test outcomes: {sorted(missing)}")
    unexpected = outcomes.keys() - set(expected_f2p + expected_p2p)
    if unexpected:
        raise ValueError(f"unexpected parsed test outcomes: {sorted(unexpected)}")
    return outcomes
