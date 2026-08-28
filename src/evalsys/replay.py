from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from .config import Settings
from .errors import EvalError
from .harness import HarnessInvocation, build_harness_command, extract_test_outcomes
from .process import ProcessTimeout, run_process
from .recovery import compute_input_fingerprint, load_reusable_run, write_completed_run
from .schema import validate_json
from .verdict import decide_verdict

RUN_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


def _docker_output(argv: Sequence[str]) -> str:
    result = run_process(argv, timeout_s=60)
    if result.returncode != 0:
        raise EvalError(f"Docker cleanup command failed: {result.stderr.strip()}", category="infra_failed")
    return result.stdout


def cleanup_run_resources(run_id: str, *, runner: Callable[[Sequence[str]], str] = _docker_output) -> dict[str, list[str]]:
    if not RUN_ID.fullmatch(run_id):
        raise ValueError("unsafe run_id")
    label = f"evalsys.run_id={run_id}"
    queries = {
        "containers": ["docker", "ps", "-aq", "--filter", f"label={label}"],
        "networks": ["docker", "network", "ls", "-q", "--filter", f"label={label}"],
        "volumes": ["docker", "volume", "ls", "-q", "--filter", f"label={label}"],
        "images": ["docker", "image", "ls", "-q", "--filter", f"label={label}"],
    }
    removals = {"containers": ["docker", "rm", "-f"], "networks": ["docker", "network", "rm"], "volumes": ["docker", "volume", "rm"], "images": ["docker", "image", "rm"]}
    found: dict[str, list[str]] = {}
    for kind, query in queries.items():
        identifiers = [line for line in runner(query).splitlines() if line]
        found[kind] = identifiers
        if identifiers:
            runner(removals[kind] + identifiers)
    return found


class EventWriter:
    def __init__(self, path: Path, run_id: str) -> None:
        self.path, self.run_id, self.sequence = path, run_id, 0

    def write(self, event: str, stage: str, details: dict) -> None:
        self.sequence += 1
        record = {"schema_version": "1.0", "run_id": self.run_id, "sequence": self.sequence, "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "event": event, "stage": stage, "details": details}
        validate_json(record, "event")
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _case_dataset(source_row: dict, destination: Path) -> None:
    destination.write_text(json.dumps([source_row], ensure_ascii=False) + "\n", encoding="utf-8")


def _prediction(instance_id: str, destination: Path) -> None:
    destination.write_text(json.dumps({"instance_id": instance_id, "model_name_or_path": "evalsys-noop", "model_patch": ""}) + "\n", encoding="utf-8")


def replay_case(settings: Settings, public_case: dict, source_row: dict, mode: str, *, run_id: str, timeout_s: int, workers: int, resume: bool) -> dict:
    if mode not in {"noop", "gold"}:
        raise EvalError(f"Unsupported replay mode: {mode}")
    unit = settings.artifact_root / "runs" / "iteration1" / run_id / public_case["instance_id"] / mode
    replay_input = {"case": public_case, "mode": mode, "harness_revision": source_row["harness_revision"], "data_revision": public_case["source_revision"], "timeout_s": timeout_s}
    input_fingerprint = compute_input_fingerprint(replay_input)
    if resume and unit.exists() and (cached := load_reusable_run(unit, input_fingerprint)) is not None:
        return cached
    if unit.exists():
        shutil.rmtree(unit)
    unit.mkdir(parents=True)
    events = EventWriter(unit / "events.jsonl", run_id)
    dataset = unit / "dataset.json"
    prediction = unit / "prediction.jsonl"
    _case_dataset(source_row, dataset)
    _prediction(public_case["instance_id"], prediction)
    report_dir = unit / "harness"
    report_dir.mkdir()
    invocation = HarnessInvocation(settings.cache_root / "swe-bench", settings.project_root / "scripts" / "official_harness_adapter.py", dataset, prediction, report_dir, run_id, public_case["instance_id"], mode, timeout_s, workers)
    command = build_harness_command(invocation)
    started = datetime.now(timezone.utc)
    began = time.monotonic()
    events.write("stage_started", "harness", {"mode": mode})
    status, classification, error = "infra_failed", "harness_failure", None
    stdout = stderr = ""
    outcomes: dict[str, str] = {}
    tests_executed = False
    try:
        result = run_process(command, timeout_s=timeout_s + 120)
        stdout, stderr = result.stdout, result.stderr
        raw_path = report_dir / "adapter-result.json"
        if result.returncode != 0 or not raw_path.is_file():
            raise EvalError(f"Official harness failed with exit {result.returncode}: {stderr[-1000:]}", category="infra_failed")
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        tests_executed = bool(raw.get("tests_executed"))
        outcomes = extract_test_outcomes(raw, public_case["FAIL_TO_PASS"], public_case["PASS_TO_PASS"], tests_executed=tests_executed)
        verdict = decide_verdict(mode, outcomes, public_case["FAIL_TO_PASS"], public_case["PASS_TO_PASS"])
        status, classification = verdict["status"], verdict["classification"]
    except ProcessTimeout as exc:
        stdout, stderr, status, classification = exc.stdout, exc.stderr, "timeout", "process_tree_timeout"
        error = {"category": "timeout", "message": str(exc), "stage": "harness"}
    except (EvalError, ValueError, OSError, json.JSONDecodeError) as exc:
        category = getattr(exc, "category", "invalid")
        status = "infra_failed" if category == "infra_failed" else "invalid"
        classification = "harness_failure" if status == "infra_failed" else "unparseable_test_results"
        error = {"category": category, "message": str(exc), "stage": "harness"}
    finally:
        try:
            cleanup_run_resources(run_id)
        except EvalError as cleanup_error:
            if status != "timeout":
                status, classification = "infra_failed", "cleanup_failure"
                error = {"category": "infra_failed", "message": str(cleanup_error), "stage": "cleanup"}
    (unit / "stdout.log").write_text(stdout, encoding="utf-8")
    (unit / "stderr.log").write_text(stderr, encoding="utf-8")
    events.write("stage_finished", "harness", {"status": status})
    ended = datetime.now(timezone.utc)
    result_record = {
        "schema_version": "1.0", "run_id": run_id, "case_id": public_case["case_id"], "instance_id": public_case["instance_id"], "split": public_case["split"], "mode": mode,
        "status": status, "classification": classification, "harness_revision": source_row["harness_revision"], "data_revision": public_case["source_revision"], "repo": public_case["repo"], "base_commit": public_case["base_commit"], "docker_image": source_row.get("docker_image", ""),
        "started_at": started.isoformat().replace("+00:00", "Z"), "ended_at": ended.isoformat().replace("+00:00", "Z"), "wall_time_s": time.monotonic() - began,
        "stages": {"environment": "passed" if status not in {"infra_failed", "timeout"} else "failed", "patch": "skipped" if mode == "noop" else ("passed" if status not in {"infra_failed", "timeout"} else "failed"), "tests": "timeout" if status == "timeout" else ("passed" if tests_executed else "failed")},
        "tests_executed": tests_executed,
        "fail_to_pass": {name: outcomes[name] for name in public_case["FAIL_TO_PASS"] if name in outcomes}, "pass_to_pass": {name: outcomes[name] for name in public_case["PASS_TO_PASS"] if name in outcomes},
        "logs": {"stdout": "stdout.log", "stderr": "stderr.log", "harness": "harness"}, "error": error,
    }
    write_completed_run(unit, result_record, input_fingerprint, ["stdout.log", "stderr.log", "events.jsonl"])
    return result_record


def replay_cases(settings: Settings, public_cases: list[dict], source_rows: dict[str, dict], mode: str, *, timeout_s: int = 1800, workers: int = 1, resume: bool = False) -> dict:
    if workers < 1 or workers > 4:
        raise EvalError("Replay workers must be between 1 and 4", hint="Use the serial default unless resources were reviewed")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"_replay_{mode}_{compute_input_fingerprint({'mode': mode, 'cases': [case['instance_id'] for case in public_cases], 'timeout': timeout_s})[:10]}"
    results = []
    for case in public_cases:
        row = dict(source_rows[case["instance_id"]])
        row["harness_revision"] = "7a21e05772954cc81471ae19d56f436cecf43c54"
        results.append(replay_case(settings, case, row, mode, run_id=run_id, timeout_s=timeout_s, workers=workers, resume=resume))
    summary = {"schema_version": "1.0", "run_id": run_id, "status": "passed" if all(result["status"] == "passed" for result in results) else "failed", "results": [{"instance_id": result["instance_id"], "mode": result["mode"], "status": result["status"], "result": f"{result['instance_id']}/{mode}/result.json"} for result in results]}
    validate_json(summary, "validation-summary")
    root = settings.artifact_root / "runs" / "iteration1" / run_id
    (root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"run_id": run_id, "run_directory": str(root), "status": summary["status"], "results": results}
