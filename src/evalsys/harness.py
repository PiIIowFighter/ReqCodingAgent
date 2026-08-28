from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path


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


def _wsl_path(path: Path) -> str:
    text = str(path.resolve())
    match = re.match(r"^([A-Za-z]):[\\/](.*)$", text)
    if not match:
        return text.replace("\\", "/")
    suffix = match.group(2).replace("\\", "/")
    return f"/mnt/{match.group(1).lower()}/{suffix}"


def build_harness_command(invocation: HarnessInvocation, *, platform_name: str = sys.platform, python_executable: str | None = None) -> list[str]:
    if invocation.mode not in {"noop", "gold"}:
        raise ValueError(f"unsupported replay mode: {invocation.mode}")
    if invocation.max_workers < 1:
        raise ValueError("max_workers must be positive")
    windows = platform_name == "win32"
    prefix = (["wsl.exe", "-e", "/home/yyt/.local/bin/python3.11"] if windows else [python_executable or "python3.11"])
    convert = _wsl_path if windows else str
    predictions = "gold" if invocation.mode == "gold" else convert(invocation.predictions_path)
    command = prefix + [
        convert(invocation.adapter_path),
        "--harness-checkout", convert(invocation.harness_checkout),
        "--dataset", convert(invocation.dataset_path),
        "--predictions", predictions,
        "--report-dir", convert(invocation.report_dir),
        "--run-id", invocation.run_id,
        "--instance-id", invocation.instance_id,
        "--timeout", str(invocation.timeout_s),
        "--max-workers", str(invocation.max_workers),
        "--label", f"evalsys.run_id={invocation.run_id}",
    ]
    if invocation.mode == "noop":
        command.append("--skip-patch")
    return command


def extract_test_outcomes(raw: dict, expected_f2p: list[str], expected_p2p: list[str], *, tests_executed: bool) -> dict[str, str]:
    if not tests_executed:
        raise ValueError("missing actual test execution marker")
    direct = raw.get("outcomes")
    if direct is not None:
        if not isinstance(direct, dict) or any(not isinstance(name, str) or status not in {"PASSED", "FAILED", "ERROR", "SKIPPED", "XFAIL"} for name, status in direct.items()):
            raise ValueError("unknown or unparseable raw test status")
        expected = set(expected_f2p + expected_p2p)
        if set(direct) != expected:
            missing, unexpected = expected - set(direct), set(direct) - expected
            raise ValueError(f"missing={sorted(missing)} unexpected={sorted(unexpected)} test outcomes")
        return dict(direct)
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
        mappings = {"success": "PASSED", "failure": "FAILED", "error": "ERROR", "skipped": "SKIPPED", "xfail": "XFAIL"}
        for bucket, status in mappings.items():
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
    expected_all = set(expected_f2p + expected_p2p)
    unexpected = outcomes.keys() - expected_all
    if unexpected:
        raise ValueError(f"unexpected parsed test outcomes: {sorted(unexpected)}")
    return outcomes
