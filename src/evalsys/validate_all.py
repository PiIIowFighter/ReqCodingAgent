from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .evidence import EvidenceRecorder
from .persistence import atomic_json
from .recovery import compute_input_fingerprint

STAGES = ("preflight", "locks_and_cache", "strict_data", "unit_tests", "isolation", "replay_noop", "replay_gold", "aggregation", "audit", "acceptance")


def _new_id() -> str:
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{now}_validate_all_{compute_input_fingerprint({'stages': STAGES})[:10]}"


def run_validate_all(project_root: Path, *, stage_runner: Callable[[str, dict[str, Any]], dict[str, Any]], run_id: str | None = None, resume: bool = False, config: dict[str, Any] | None = None, artifact_root: Path | None = None) -> dict[str, Any]:
    root = project_root.resolve()
    identity = run_id or _new_id()
    directory = (artifact_root or (root / "artifacts")).resolve() / "runs/iteration1" / identity
    state_path = directory / "validate-all-state.json"
    recorder = EvidenceRecorder(root, iteration=1, raw_root=directory.parent)
    evidence_config = config or {}
    if resume:
        if not state_path.is_file():
            raise FileNotFoundError(f"validate-all state does not exist: {identity}")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if config is not None and state.get("config") != config:
            raise ValueError("resume configuration differs from durable state")
        evidence = recorder.start_explicit(identity, "validate_all", evidence_config, ["python", "-m", "evalsys.cli", "validate-all", "--resume", "--run-id", identity], existing_raw_dir=directory, resume=True)
    else:
        directory.mkdir(parents=True, exist_ok=False)
        evidence = recorder.start_explicit(identity, "validate_all", evidence_config, ["python", "-m", "evalsys.cli", "validate-all"], existing_raw_dir=directory)
        state = {"schema_version": "1.0", "run_id": identity, "status": "running", "config": evidence_config, "stages": {}, "children": []}
        atomic_json(state_path, state)
    state["resume_verified"] = bool(resume)
    test_failure = False
    try:
        for name in STAGES:
            if state["stages"].get(name, {}).get("status") == "passed":
                continue
            prior_children = [child for child in state["children"] if child.get("run_type") == name and child.get("validity") == "active"]
            evidence.record_event("stage_started", {"stage": name})
            result = stage_runner(name, {"run_id": identity, "run_directory": directory, "state": state, "resume_child": prior_children[-1] if prior_children else None})
            state["stages"][name] = result
            if name.startswith("replay_") and result.get("run_id") and not any(child.get("run_id") == result["run_id"] for child in state["children"]):
                supersedes = result.get("supersedes", [child["run_id"] for child in prior_children])
                state["children"].append({"run_id": result["run_id"], "run_type": name, "run_directory": result["run_directory"], "validity": "active", "supersedes": supersedes, "attempts": result.get("attempts", 1)})
            atomic_json(state_path, state)
            evidence.record_event("stage_finished", {"stage": name, "status": result.get("status")})
            if result.get("status") != "passed":
                if result.get("failure_kind") == "test" or (test_failure and name in {"aggregation", "audit", "acceptance"}):
                    test_failure = True
                    continue
                state["status"] = "failed"
                atomic_json(state_path, state)
                evidence.fail({"status": "failed", "failed": 1, "passed": 0, "classification": "infra_stage_failure", "reason": name})
                return state
        state["status"] = "failed" if test_failure else "passed"
        atomic_json(state_path, state)
        result = {"status": state["status"], "passed": int(state["status"] == "passed"), "failed": int(state["status"] != "passed"), "classification": "test_failure" if test_failure else "validated"}
        (evidence.finish if state["status"] == "passed" else evidence.fail)(result)
        return state
    except Exception as exc:
        evidence.fail({"status": "failed", "failed": 1, "passed": 0, "classification": "validate_all_exception", "reason": str(exc)})
        raise
