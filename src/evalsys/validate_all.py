from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .persistence import atomic_json
from .recovery import compute_input_fingerprint

STAGES = ("preflight", "locks_and_cache", "strict_data", "isolation", "replay_noop", "replay_gold", "aggregation", "audit", "acceptance")


def _new_id() -> str:
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{now}_validate_all_{compute_input_fingerprint({'stages': STAGES})[:10]}"


def run_validate_all(project_root: Path, *, stage_runner: Callable[[str, dict[str, Any]], dict[str, Any]], run_id: str | None = None, resume: bool = False, config: dict[str, Any] | None = None) -> dict[str, Any]:
    root = project_root.resolve()
    identity = run_id or _new_id()
    directory = root / "artifacts/runs/iteration1" / identity
    state_path = directory / "validate-all-state.json"
    if resume:
        if not state_path.is_file():
            raise FileNotFoundError(f"validate-all state does not exist: {identity}")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if config is not None and state.get("config") != config:
            raise ValueError("resume configuration differs from durable state")
    else:
        directory.mkdir(parents=True, exist_ok=False)
        state = {"schema_version": "1.0", "run_id": identity, "status": "running", "config": config or {}, "stages": {}, "children": []}
        atomic_json(state_path, state)
    test_failure = False
    for name in STAGES:
        if state["stages"].get(name, {}).get("status") == "passed":
            continue
        result = stage_runner(name, {"run_id": identity, "run_directory": directory, "state": state})
        state["stages"][name] = result
        if name.startswith("replay_") and result.get("run_id") and not any(child.get("run_id") == result["run_id"] for child in state["children"]):
            state["children"].append({"run_id": result["run_id"], "run_type": name, "run_directory": result["run_directory"], "validity": "active", "supersedes": result.get("supersedes", []), "attempts": result.get("attempts", 1)})
        atomic_json(state_path, state)
        if result.get("status") != "passed":
            if result.get("failure_kind") == "test" or (test_failure and name in {"aggregation", "audit", "acceptance"}):
                test_failure = True
                continue
            state["status"] = "failed"
            atomic_json(state_path, state)
            return state
    state["status"] = "failed" if test_failure else "passed"
    atomic_json(state_path, state)
    return state
