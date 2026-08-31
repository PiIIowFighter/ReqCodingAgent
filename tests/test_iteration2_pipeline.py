from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from evalsys.agent_runner import AgentRunRequest
from evalsys.baseline import FORMAL_SEED, build_formal_plan, verify_formal_plan
from evalsys.cli import build_parser, exit_code_for_status
from evalsys.errors import EvalError
from evalsys.config import Settings
from evalsys.evidence import EvidenceRecorder, sanitize, select_current_runs, verify_active_audit_runs, verify_audit_index_metadata
from evalsys.harness import HarnessInvocation, build_harness_command
from evalsys.harness_environment import verify_harness_environment, verify_harness_environment_receipt
from evalsys.verdict import decide_verdict
from evalsys.replay import classify_agent_harness_failure
from evalsys.iteration2 import (
    FORMAL_INSTANCES,
    build_agent_container_command,
    development_cells,
    prepare_task_repository,
    cell_resume_action,
    classify_cell_for_resume,
    freeze_baseline,
    ensure_experiment_manifest,
    experiment_status,
    extract_actual_model,
    record_experiment_cell,
    reconcile_completed_cell,
    start_infra_retry,
    verify_cell_evidence,
    official_image_name,
    resolve_image_identity,
    select_public_case,
    summarize_formal_results,
    summarize_audited_formal_results,
    behavior_tree_hash,
    current_tool_schema_bytes,
    load_provider_identity,
    load_completed_agent_result,
    select_evaluation_attempt,
    verify_runtime_provider,
    load_formal_results,
    run_isolation_diagnostic,
    deterministic_waves,
    run_bounded_wave,
    WaveExecutionError,
    write_isolation_proof,
    write_test_receipt,
    run_development,
    verify_development_evidence,
    verify_development_record_cells,
    verify_gate_receipt,
    verify_git_gate,
    verify_frozen_baseline,
)
from reqagent.config import AgentConfig
from reqagent.context import ContextLedger
from reqagent.loop import AgentLoop
from reqagent.model import ScriptedModel
from reqagent.tools import build_registry
from reqagent.tools.command import LocalTestCommandExecutor
from reqagent.trace import RunStore
from reqagent.cli import _execute
from reqagent.workspace import GitWorkspace


ROOT = Path(__file__).resolve().parents[1]


def _records() -> list[dict]:
    return [json.loads(line) for line in (ROOT / "benchmark/manifests/paired-cases.jsonl").read_text(encoding="utf-8").splitlines()]


def test_development_cells_are_exact_three_by_two_matrix():
    cells = development_cells(_records())
    assert [(cell["instance_id"], cell["prompt_variant"]) for cell in cells] == [
        ("django__django-11133", "full"), ("django__django-11133", "fuzzy"),
        ("scikit-learn__scikit-learn-14983", "full"), ("scikit-learn__scikit-learn-14983", "fuzzy"),
        ("matplotlib__matplotlib-25332", "full"), ("matplotlib__matplotlib-25332", "fuzzy"),
    ]


def test_iteration2_cli_rejects_resume_without_run_id():
    parser = build_parser()
    args = parser.parse_args(["agent-run", "--case-id", "D-O1", "--variant", "full", "--config", "agent.json", "--confirm", "--resume"])
    assert args.resume and args.run_id is None


def test_iteration2_cli_has_required_protected_arguments():
    parser = build_parser()
    agent = parser.parse_args(["agent-run", "--case-id", "D-O1", "--variant", "full", "--config", "agent.json", "--confirm"])
    assert agent.case_id == "D-O1" and agent.variant == "full"
    dev = parser.parse_args(["run-dev", "--version", "v001", "--config", "agent.json", "--confirm"])
    assert dev.version == "v001"
    freeze = parser.parse_args(["freeze-baseline", "--name", "baseline-v1", "--dev-version", "v001", "--config", "agent.json", "--confirm"])
    assert freeze.dev_version == "v001"
    formal = parser.parse_args(["run-formal", "--name", "baseline-v1", "--confirm", "--resume"])
    assert formal.resume and not hasattr(formal, "config") and not hasattr(formal, "variant")
    report = parser.parse_args(["report", "--name", "baseline-v1"])
    assert report.name == "baseline-v1"
    status = parser.parse_args(["experiment-status", "--kind", "dev", "--name", "v002"])
    assert status.kind == "dev" and status.name == "v002"
    limited = parser.parse_args(["run-dev", "--version", "v003", "--config", "agent.json", "--parallel-cells", "2", "--max-new-cells", "4", "--confirm"])
    assert limited.max_new_cells == 4 and limited.parallel_cells == 2


def test_cli_paused_status_returns_zero():
    assert exit_code_for_status("paused") == 0
    assert exit_code_for_status("passed") == 0
    assert exit_code_for_status("failed") == 1


def test_formal_plan_uses_canonical_spec_order_before_shuffle():
    test_records = [record for record in _records() if record["split"] == "test"]
    plan = build_formal_plan(list(reversed(test_records)), seed=FORMAL_SEED)
    assert [(plan[index]["instance_id"], plan[index]["variant"], plan[index + 1]["variant"]) for index in range(0, 24, 2)] == [
        ("psf__requests-2317", "fuzzy", "full"),
        ("pytest-dev__pytest-7432", "full", "fuzzy"),
        ("scikit-learn__scikit-learn-13439", "fuzzy", "full"),
        ("astropy__astropy-14995", "fuzzy", "full"),
        ("pydata__xarray-4094", "fuzzy", "full"),
        ("django__django-13933", "full", "fuzzy"),
        ("scikit-learn__scikit-learn-13779", "full", "fuzzy"),
        ("sphinx-doc__sphinx-8595", "fuzzy", "full"),
        ("django__django-10914", "full", "fuzzy"),
        ("matplotlib__matplotlib-25311", "full", "fuzzy"),
        ("sphinx-doc__sphinx-8721", "fuzzy", "full"),
        ("matplotlib__matplotlib-23476", "full", "fuzzy"),
    ]


def test_formal_plan_is_deterministic_paired_and_balanced():
    test_records = [record for record in _records() if record["split"] == "test"]
    first = build_formal_plan(test_records, seed=FORMAL_SEED)
    second = build_formal_plan(list(reversed(test_records)), seed=FORMAL_SEED)
    assert first == second
    assert len(first) == 24
    assert [cell["sequence"] for cell in first] == list(range(1, 25))
    for offset in range(0, 24, 2):
        pair = first[offset:offset + 2]
        assert pair[0]["instance_id"] == pair[1]["instance_id"]
        assert {pair[0]["variant"], pair[1]["variant"]} == {"full", "fuzzy"}
    assert sum(first[index]["variant"] == "full" for index in range(0, 24, 2)) == 6
    verify_formal_plan(first, test_records, seed=FORMAL_SEED)


def test_deterministic_waves_keep_pair_order_and_bound_parallelism():
    plan = [
        {"sequence": 1, "instance_id": "a", "case_id": "a-full", "variant": "full"},
        {"sequence": 2, "instance_id": "a", "case_id": "a-fuzzy", "variant": "fuzzy"},
        {"sequence": 3, "instance_id": "b", "case_id": "b-fuzzy", "variant": "fuzzy"},
        {"sequence": 4, "instance_id": "b", "case_id": "b-full", "variant": "full"},
        {"sequence": 5, "instance_id": "c", "case_id": "c-full", "variant": "full"},
        {"sequence": 6, "instance_id": "c", "case_id": "c-fuzzy", "variant": "fuzzy"},
    ]
    waves = deterministic_waves(plan, parallel_cells=2)
    assert [[cell["case_id"] for cell in wave] for wave in waves] == [
        ["a-full", "b-fuzzy"], ["a-fuzzy", "b-full"], ["c-full"], ["c-fuzzy"],
    ]
    assert all(len(wave) <= 2 and len({cell["instance_id"] for cell in wave}) == len(wave) for wave in waves)


def test_bounded_wave_runs_two_different_pairs_concurrently_and_sorts_results():
    import threading, time
    active = maximum = 0
    lock = threading.Lock()
    cells = [{"sequence": 2, "instance_id": "b"}, {"sequence": 1, "instance_id": "a"}]
    def worker(cell):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return {**cell, "status": "unresolved"}
    results = run_bounded_wave(cells, worker, parallel_cells=2)
    assert maximum == 2
    assert [row["sequence"] for row in results] == [1, 2]


def test_bounded_wave_waits_for_other_worker_and_preserves_result_on_failure():
    cells = [{"sequence": 1, "instance_id": "a"}, {"sequence": 2, "instance_id": "b"}]
    completed = []
    def worker(cell):
        if cell["instance_id"] == "a":
            raise RuntimeError("worker failed")
        completed.append(cell["instance_id"])
        return {**cell, "status": "unresolved"}
    with pytest.raises(WaveExecutionError) as caught:
        run_bounded_wave(cells, worker, parallel_cells=2)
    assert completed == ["b"]
    assert [row["instance_id"] for row in caught.value.results] == ["b"]


def test_formal_plan_rejects_identity_or_order_tampering():
    records = [record for record in _records() if record["split"] == "test"]
    plan = build_formal_plan(records, seed=FORMAL_SEED)
    plan[0]["variant"] = plan[1]["variant"]
    with pytest.raises(EvalError, match="formal plan"):
        verify_formal_plan(plan, records, seed=FORMAL_SEED)


def test_select_public_case_accepts_case_or_instance_but_requires_exact_variant():
    records = _records()
    selected = select_public_case(records, "D-O1", "full", allowed_split="dev")
    assert selected["instance_id"] == "django__django-11133" and selected["prompt_variant"] == "full"
    assert select_public_case(records, "django__django-11133", "fuzzy", allowed_split="dev")["case_id"] == "D-O1-fuzzy"
    with pytest.raises(EvalError, match="not found"):
        select_public_case(records, "T-O1", "full", allowed_split="dev")


def test_agent_handoff_accepts_prompt_field_and_hides_experiment_identity(tmp_path: Path):
    case = {
        "prompt": "Fix it", "base_commit": "a" * 40, "case_id": "T-O1-full",
        "instance_id": "secret-id", "split": "test", "prompt_variant": "fuzzy",
        "ambiguity_type": "omission", "FAIL_TO_PASS": ["secret test"], "PASS_TO_PASS": [],
    }
    projected = AgentRunRequest.from_public_case(case, tmp_path).to_agent_input()
    assert projected == {"task": "Fix it", "repository": str(tmp_path.resolve()), "base_commit": "a" * 40}
    serialized = json.dumps(projected).lower()
    assert all(word not in serialized for word in ("fuzzy", "omission", "secret-id", "secret test"))


def test_agent_evaluator_harness_applies_prediction_patch_with_frozen_image(tmp_path: Path):
    invocation = HarnessInvocation(tmp_path, tmp_path / "adapter.py", tmp_path / "dataset.json", tmp_path / "prediction.jsonl", tmp_path / "report", "run", "instance", "agent", 1800, frozen_image="repo/task@sha256:" + "a" * 64)
    command = build_harness_command(invocation, platform_name="linux", python_executable="python")
    assert "--skip-patch" not in command
    assert command[command.index("--predictions") + 1] == str(invocation.predictions_path)
    assert command[command.index("--frozen-image") + 1] == invocation.frozen_image


def test_legacy_invalid_output_budget_migrates_to_both_limits(tmp_path: Path):
    raw = json.loads((ROOT / "configs/agent/offline-scripted.json").read_text(encoding="utf-8"))
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = AgentConfig.load(path)
    assert "max_invalid_outputs" not in config.budgets
    assert config.budgets["max_consecutive_invalid_outputs"] == 6
    assert config.budgets["max_total_invalid_outputs"] == 6


def test_live_config_has_independent_invalid_output_limits(tmp_path: Path):
    raw = json.loads((ROOT / "configs/agent/live-local-proxy.json").read_text(encoding="utf-8"))
    raw["budgets"].pop("max_invalid_outputs", None)
    raw["budgets"]["max_consecutive_invalid_outputs"] = 3
    raw["budgets"]["max_total_invalid_outputs"] = 6
    raw["budgets"].update({"max_steps": 30, "max_tool_calls": 60, "wall_clock_seconds": 1800, "command_timeout_seconds": 300, "max_retries": 3})
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = AgentConfig.load(path)
    assert config.budgets["max_consecutive_invalid_outputs"] == 3
    assert config.budgets["max_total_invalid_outputs"] == 6


def test_official_image_name_matches_pinned_harness_convention():
    assert official_image_name("django__django-11133") == "swebench/sweb.eval.x86_64.django_1776_django-11133:latest"
    assert official_image_name("scikit-learn__scikit-learn-14983") == "swebench/sweb.eval.x86_64.scikit-learn_1776_scikit-learn-14983:latest"


def test_image_identity_records_id_and_digest():
    def runner(argv, **kwargs):
        payload = [{"Id": "sha256:" + "a" * 64, "RepoDigests": ["swebench/task@sha256:" + "b" * 64]}]
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
    identity = resolve_image_identity("swebench/task:latest", docker_prefix=["docker"], runner=runner)
    assert identity["image_id"] == "sha256:" + "a" * 64
    assert identity["pinned"] == "swebench/task@sha256:" + "b" * 64


def test_prepare_task_repository_exports_only_testbed_at_base_commit(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "-C", str(source), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
    (source / "tracked.txt").write_text("content\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "initial"], check=True)
    base_commit = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    captured = []
    def runner(argv, **kwargs):
        captured.append(argv)
        staging = Path(argv[argv.index("--mount") + 1].split("src=", 1)[1].split(",", 1)[0])
        subprocess.run(["git", "-C", str(source), "bundle", "create", str(staging / "run-1.bundle"), "--all"], check=True)
        return subprocess.CompletedProcess(argv, 0, "", "")
    destination = tmp_path / "staging" / "task"
    result = prepare_task_repository(
        docker_prefix=["docker"], image="sha256:" + "a" * 64,
        base_commit=base_commit, destination=destination, run_id="run-1", runner=runner,
    )
    assert result == destination.resolve()
    assert (destination / "tracked.txt").read_text(encoding="utf-8") == "content\n"
    assert not (destination.parent / "run-1.bundle").exists()
    command = captured[0]
    assert command[command.index("--pull") + 1] == "never"
    assert command[command.index("--network") + 1] == "none"
    assert "cp -a" not in command[-1] and "cp -R" not in command[-1]
    assert "git -C /testbed bundle create /export/run-1.bundle --all" in command[-1]


def test_agent_container_command_is_task_image_only_and_hardened(tmp_path: Path):
    command = build_agent_container_command(
        docker_prefix=["docker"], image="sweb.eval.x86_64.demo@sha256:" + "a" * 64,
        workspace=tmp_path, run_id="run-1", shell_command="pytest -q", timeout_seconds=300,
    )
    assert command[:2] == ["docker", "run"]
    assert command[command.index("--pull") + 1] == "never"
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges" in command
    assert "/var/run/docker.sock" not in " ".join(command)
    assert [command[index + 1] for index, value in enumerate(command) if value == "--mount"] == [f"type=bind,src={tmp_path.resolve()},dst=/workspace"]


def test_resume_never_retries_valid_agent_outcomes():
    for status in ("resolved", "unresolved", "agent_no_patch", "agent_stopped", "model_error"):
        assert classify_cell_for_resume({"status": status, "evaluator_recorded": True}) == "complete"
    assert classify_cell_for_resume({"status": "eval_infra_failed", "evaluator_recorded": True}) == "retryable_infra"
    assert classify_cell_for_resume(None) == "not_started"


def test_harness_failures_default_to_valid_unresolved():
    for raw, classification in (
        ({"status": "invalid", "classification": "official_patch_apply_failure", "stage": "patch"}, "agent_patch_apply_failed"),
        ({"status": "timeout", "classification": "official_tests_timeout", "stage": "tests"}, "tests_timeout"),
        ({"status": "infra_failed", "classification": "official_tests_error", "stage": "tests"}, "tests_error"),
        ({"status": "invalid", "classification": "unknown", "stage": "unknown"}, "tests_unresolved"),
    ):
        result = classify_agent_harness_failure(raw)
        assert result == {"status": "unresolved", "classification": classification}
    assert classify_agent_harness_failure({"status": "infra_failed", "classification": "missing_instance_log", "stage": "environment"}) == {"status": "eval_infra_failed", "classification": "missing_instance_log"}
    assert classify_agent_harness_failure({"status": "infra_failed", "classification": "cleanup_failure", "stage": "cleanup"}) == {"status": "eval_infra_failed", "classification": "cleanup_failure"}


def test_agent_patch_and_test_failures_are_valid_unresolved():
    patch_apply = decide_verdict("agent", {}, [], [], failure_kind="patch_apply")
    assert patch_apply == {"status": "unresolved", "classification": "agent_patch_apply_failed", "fail_to_pass": {}, "pass_to_pass": {}}
    timeout = decide_verdict("agent", {}, [], [], failure_kind="test_timeout")
    assert timeout["status"] == "unresolved" and timeout["classification"] == "tests_timeout"
    error = decide_verdict("agent", {"fixed": "ERROR"}, ["fixed"], [])
    assert error["status"] == "unresolved" and error["classification"] == "tests_error"
    for result in (patch_apply, timeout, error):
        assert classify_cell_for_resume({**result, "evaluator_recorded": True}) == "complete"


def test_completed_cell_reuse_requires_explicit_resume():
    complete = {"status": "unresolved", "evaluator_recorded": True}
    with pytest.raises(EvalError, match="explicit --resume"):
        cell_resume_action(complete, resume=False)
    assert cell_resume_action(complete, resume=True) == "reuse"
    assert cell_resume_action(None, resume=False) == "start"


def test_experiment_manifest_is_stable_and_rejects_drift(tmp_path: Path):
    root = tmp_path / "formal"
    manifest = {"baseline": "baseline-v1", "plan_sha256": "a" * 64, "cells": 24}
    assert ensure_experiment_manifest(root, manifest) == root / "experiment-manifest.json"
    assert ensure_experiment_manifest(root, manifest) == root / "experiment-manifest.json"
    with pytest.raises(EvalError, match="manifest mismatch"):
        ensure_experiment_manifest(root, {**manifest, "cells": 23})


def test_experiment_status_reports_counts_current_run_and_resume_command(tmp_path: Path):
    root = tmp_path / "dev/v002"
    ensure_experiment_manifest(root, {
        "schema_version": "1.0", "version": "v002", "cells": 3,
        "plan": [
            {"case_id": "D-1", "instance_id": "one", "variant": "full"},
            {"case_id": "D-2", "instance_id": "one", "variant": "fuzzy"},
            {"case_id": "D-3", "instance_id": "two", "variant": "full"},
        ],
    })
    record_experiment_cell(root, "D-1", "run-complete", "complete")
    record_experiment_cell(root, "D-2", "run-pending", "pending")
    status = experiment_status(root, kind="dev", name="v002")
    assert status["counts"] == {"completed": 1, "pending": 1, "not_started": 1}
    assert status["current"] == {"case_id": "D-2", "run_id": "run-pending", "state": "pending"}
    assert status["can_resume"] is True
    assert status["resume_command"] == "evalsys run-dev --version v002 --config CONFIG --resume --confirm"


def test_concurrent_manifest_updates_keep_both_cells(tmp_path: Path):
    import threading
    root = tmp_path / "formal"
    ensure_experiment_manifest(root, {"baseline": "baseline-v1", "cells": 2})
    barrier = threading.Barrier(2)
    def update(case_id):
        barrier.wait()
        record_experiment_cell(root, case_id, f"run-{case_id}", "complete")
    threads = [threading.Thread(target=update, args=(case_id,)) for case_id in ("a", "b")]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    manifest = json.loads((root / "experiment-manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["cell_runs"]) == {"a", "b"}


def test_concurrent_audit_index_updates_keep_both_runs(tmp_path: Path):
    import threading
    project = tmp_path / "project"
    project.mkdir()
    recorder = EvidenceRecorder(project, iteration=2)
    runs = [recorder.start("formal_cell", {"cell": name}, ["formal"]) for name in ("a", "b")]
    barrier = threading.Barrier(2)
    def finish(run):
        barrier.wait()
        run.finish({"status": "unresolved", "classification": "tests_failed"})
    threads = [threading.Thread(target=finish, args=(run,)) for run in runs]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    index = json.loads((project / "audit/iteration2/index.json").read_text(encoding="utf-8"))
    assert {entry["run_id"] for entry in index["runs"]} == {run.run_id for run in runs}


def test_experiment_manifest_tracks_pending_and_completed_run_ids(tmp_path: Path):
    root = tmp_path / "formal"
    ensure_experiment_manifest(root, {"baseline": "baseline-v1", "cells": 24})
    record_experiment_cell(root, "T-O1-full", "run-pending", "pending")
    manifest = json.loads((root / "experiment-manifest.json").read_text(encoding="utf-8"))
    assert manifest["cell_runs"]["T-O1-full"] == {"run_id": "run-pending", "state": "pending"}
    record_experiment_cell(root, "T-O1-full", "run-pending", "complete")
    manifest = json.loads((root / "experiment-manifest.json").read_text(encoding="utf-8"))
    assert manifest["cell_runs"]["T-O1-full"]["state"] == "complete"
    with pytest.raises(EvalError, match="run_id"):
        record_experiment_cell(root, "T-O1-full", "different", "complete")


def test_completed_cell_evidence_requires_complete_and_cell_checksum(tmp_path: Path):
    raw = tmp_path / "run"
    raw.mkdir()
    cell = {"run_id": "run-1", "status": "unresolved", "evaluator_recorded": True}
    (raw / "cell-result.json").write_text(json.dumps(cell), encoding="utf-8")
    digest = __import__("hashlib").sha256((raw / "cell-result.json").read_bytes()).hexdigest()
    (raw / "checksums.sha256").write_text(f"{digest}  cell-result.json\n", encoding="utf-8")
    (raw / "COMPLETE").write_text("complete\n", encoding="utf-8")
    assert verify_cell_evidence(raw, expected_run_id="run-1") == cell
    (raw / "COMPLETE").unlink()
    with pytest.raises(EvalError, match="COMPLETE"):
        verify_cell_evidence(raw, expected_run_id="run-1")


def test_raw_complete_reconciliation_copies_result_and_repairs_manifest(tmp_path: Path):
    project = tmp_path / "project"
    root = project / "artifacts/runs/iteration2/dev/v002"
    raw = project / "artifacts/runs/iteration2/run-1"
    raw.mkdir(parents=True)
    ensure_experiment_manifest(root, {
        "schema_version": "1.0", "version": "v002", "cells": 1,
        "plan": [{"case_id": "D-1", "instance_id": "one", "variant": "full"}],
    })
    record_experiment_cell(root, "D-1", "run-1", "pending")
    cell = {"run_id": "run-1", "case_id": "D-1", "instance_id": "one", "variant": "full", "status": "unresolved", "evaluator_recorded": True}
    (raw / "cell-result.json").write_text(json.dumps(cell), encoding="utf-8")
    digest = __import__("hashlib").sha256((raw / "cell-result.json").read_bytes()).hexdigest()
    (raw / "checksums.sha256").write_text(f"{digest}  cell-result.json\n", encoding="utf-8")
    (raw / "COMPLETE").write_text("complete\n", encoding="utf-8")
    destination = root / "cells/D-1/cell-result.json"
    result = reconcile_completed_cell(root, destination, raw, case_id="D-1", expected={"case_id": "D-1", "instance_id": "one", "variant": "full"})
    assert result["run_id"] == "run-1" and destination.is_file()
    manifest = json.loads((root / "experiment-manifest.json").read_text(encoding="utf-8"))
    assert manifest["cell_runs"]["D-1"] == {"run_id": "run-1", "state": "complete"}


def test_evaluator_reuses_complete_attempt_or_allocates_new_attempt(tmp_path: Path):
    evaluation = tmp_path / "evaluation"
    assert select_evaluation_attempt(evaluation) == (evaluation, False)
    evaluation.mkdir()
    (evaluation / "input-fingerprint.json").write_text("{}", encoding="utf-8")
    attempt, resume = select_evaluation_attempt(evaluation)
    assert attempt.parent == tmp_path and attempt.name.startswith("evaluation-attempt-") and resume is False
    (evaluation / "COMPLETE.json").write_text("{}", encoding="utf-8")
    assert select_evaluation_attempt(evaluation) == (evaluation, True)


def test_completed_agent_result_reuses_existing_patch_without_model_call(tmp_path: Path):
    run = tmp_path / "run"
    run.mkdir()
    patch = "diff --git a/x b/x\n"
    (run / "agent.patch").write_text(patch, encoding="utf-8")
    result = {"run_id": "run-1", "stop_reason": "submitted", "submitted": {"tests": []}, "usage": {}, "steps": 2, "tool_calls": 3, "patch": {"sha256": __import__("hashlib").sha256(patch.encode()).hexdigest(), "files": 1, "additions": 1, "deletions": 0, "bytes": len(patch)}, "warnings": []}
    (run / "agent-result.json").write_text(json.dumps(result), encoding="utf-8")
    (run / "AGENT_COMPLETE").write_text("complete\n", encoding="utf-8")
    loaded = load_completed_agent_result(run, expected_run_id="run-1")
    assert loaded.run_id == "run-1" and loaded.patch.text == patch
    (run / "agent.patch").write_text("tampered", encoding="utf-8")
    with pytest.raises(EvalError, match="patch checksum"):
        load_completed_agent_result(run, expected_run_id="run-1")


def test_pending_evidence_reopens_same_run_without_overwrite(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    recorder = EvidenceRecorder(project, iteration=2)
    run = recorder.start("formal_cell", {"cell": "T-O1-full"}, ["evalsys", "run-formal"])
    (run.raw_dir / "LATEST").write_text("000001.json\n", encoding="utf-8")
    resumed = recorder.resume_pending(run.run_id, "formal_cell", {"cell": "T-O1-full"}, ["evalsys", "run-formal"])
    assert resumed.run_id == run.run_id and resumed.raw_dir == run.raw_dir
    assert json.loads((run.raw_dir / "run-manifest.json").read_text(encoding="utf-8"))["run_id"] == run.run_id
    (run.raw_dir / "COMPLETE").write_text("done\n", encoding="utf-8")
    with pytest.raises(ValueError, match="terminal"):
        recorder.resume_pending(run.run_id, "formal_cell", {"cell": "T-O1-full"}, ["evalsys", "run-formal"])


def test_evaluator_infra_retry_creates_superseding_evidence(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    recorder = EvidenceRecorder(project, iteration=2)
    old = recorder.start("formal_cell", {"cell": "T-O1-full"}, ["evalsys", "run-formal"])
    old.finish({"status": "eval_infra_failed", "classification": "docker_engine"})
    retry = start_infra_retry(recorder, {"run_id": old.run_id, "status": "eval_infra_failed", "evaluator_recorded": True}, {"cell": "T-O1-full"}, ["evalsys", "run-formal"])
    assert retry.run_id != old.run_id and retry.supersedes == [old.run_id]
    retry.finish({"status": "unresolved", "classification": "tests_failed"})
    index = json.loads((project / "audit/iteration2/index.json").read_text(encoding="utf-8"))
    assert [entry["run_id"] for entry in select_current_runs(index["runs"])] == [retry.run_id]
    assert verify_active_audit_runs(project, iteration=2) == {old.run_id: [], retry.run_id: []}
    assert verify_audit_index_metadata(project, iteration=2) == []


def test_actual_returned_model_is_extracted_from_agent_events(tmp_path: Path):
    events = tmp_path / "events.jsonl"
    events.write_text(json.dumps({"kind": "model_response", "response": {"actual_model": "gpt-5.6-sol-build-42"}}) + "\n", encoding="utf-8")
    assert extract_actual_model(events) == "gpt-5.6-sol-build-42"
    events.write_text("", encoding="utf-8")
    assert extract_actual_model(events) == "unavailable"


def test_iteration2_evidence_sanitizes_cell_result_and_keeps_legacy_index(tmp_path: Path):
    project = tmp_path / "project"
    legacy = {"schema_version": 1, "runs": [{"run_id": "legacy", "kind": "offline", "status": "passed", "summary": "runs/legacy/summary.json"}]}
    (project / "audit/iteration2").mkdir(parents=True)
    (project / "audit/iteration2/index.json").write_text(json.dumps(legacy), encoding="utf-8")
    recorder = EvidenceRecorder(project, iteration=2)
    run = recorder.start("integration", {"token": "secret", "path": str(project / "private")}, ["evalsys", "integration"])
    (run.raw_dir / "cell-result.json").write_text(json.dumps({"status": "unresolved"}), encoding="utf-8")
    run.finish({"status": "unresolved", "classification": "tests_failed", "reason": f"token=abc {project / 'private'}"})
    index = json.loads((project / "audit/iteration2/index.json").read_text(encoding="utf-8"))
    assert index["runs"][0] == legacy["runs"][0]
    public = (run.audit_dir / "result-summary.json").read_text(encoding="utf-8")
    assert "token=abc" not in public and str(project) not in public
    assert "cell-result.json" in (run.raw_dir / "checksums.sha256").read_text(encoding="utf-8")


def test_formal_report_counts_pairs_categories_and_usage():
    rows = []
    categories = ["omission"] * 4 + ["specificity_reduction"] * 4 + ["referential_ambiguity"] * 4
    for index, category in enumerate(categories):
        for variant in ("full", "fuzzy"):
            rows.append({
                "instance_id": f"case-{index}", "variant": variant, "ambiguity_type": None if variant == "full" else category,
                "status": "resolved" if variant == "full" or index == 0 else "unresolved",
                "run_id": f"run-{index}-{variant}", "steps": 2, "tool_calls": 3,
                "usage": {"input_tokens": 10, "output_tokens": 4}, "wall_time_seconds": 5,
                "patch": {"files": 1, "additions": 1, "deletions": 0, "bytes": 20},
                "stop_reason": "submitted", "agent_tests": [], "evaluator_recorded": True,
                "evaluation": {"classification": "resolved" if variant == "full" else "tests_failed"},
            })
    report = summarize_formal_results(rows)
    assert report["E1_resolved"] == {"count": 12, "total": 12}
    assert report["E2_resolved"] == {"count": 1, "total": 12}
    assert report["absolute_drop"] == 11
    assert report["paired_outcomes"] == {"both": 1, "full_only": 11, "fuzzy_only": 0, "neither": 0}
    assert report["categories"]["full"]["E1"] == {"count": 12, "total": 12}
    assert report["categories"]["omission"]["E2"] == {"count": 1, "total": 4}
    assert sum(values[experiment]["total"] for values in report["categories"].values() for experiment in ("E1", "E2")) == 24
    assert all(isinstance(key, str) for key in report for _ in (0,))
    json.dumps(report, sort_keys=True)
    invalid_category = [dict(row) for row in rows]
    invalid_category[1]["ambiguity_type"] = None
    with pytest.raises(EvalError, match="ambiguity type"):
        summarize_formal_results(invalid_category)
    assert report["classifications"] == {"resolved": 12, "tests_failed": 12}
    assert report["stop_reasons"] == {"submitted": 24}
    assert report["usage"] == {"input_tokens": 240, "output_tokens": 96, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    assert report["totals"]["steps"] == 48 and report["totals"]["tool_calls"] == 72
    assert report["totals"]["wall_time_seconds"] == 120
    assert report["patch"] == {"files": 24, "additions": 24, "deletions": 0, "bytes": 480}
    assert report["infrastructure"] == {"superseded_runs": 0, "infra_failures": 0}

    baseline = {
        "name": "baseline-v1", "behavior_tree_sha256": "f" * 64,
        "config_sha256": "c" * 64, "plan_sha256": "p" * 64,
        "provider_identity": {"actual_model": "gpt-5.6-sol"},
    }
    manifest = {
        "baseline": "baseline-v1", "behavior_tree_sha256": "f" * 64,
        "plan_sha256": "p" * 64,
        "cell_runs": {row["run_id"]: {"run_id": row["run_id"], "state": "complete"} for row in rows},
    }
    identities = {
        row["run_id"]: {
            "behavior_tree_sha256": "f" * 64, "config_hash": "c" * 64,
            "baseline": "baseline-v1", "plan_sha256": "p" * 64,
            "actual_model": "gpt-5.6-sol",
        }
        for row in rows
    }
    with pytest.raises(EvalError, match="explicit"):
        summarize_audited_formal_results(rows, baseline=baseline, manifest=manifest, cell_identities=identities, reporter_behavior_tree_sha256="r" * 64, reporter_commit="a" * 40, allow_report_only_hotfix=False, confirmed=True)
    incomplete_manifest = {**manifest, "cell_runs": dict(list(manifest["cell_runs"].items())[:-1])}
    with pytest.raises(EvalError, match="24 complete"):
        summarize_audited_formal_results(rows, baseline=baseline, manifest=incomplete_manifest, cell_identities=identities, reporter_behavior_tree_sha256="r" * 64, reporter_commit="a" * 40, allow_report_only_hotfix=True, confirmed=True)
    drifted = {key: dict(value) for key, value in identities.items()}
    drifted[rows[0]["run_id"]]["baseline"] = "other"
    with pytest.raises(EvalError, match="cell identity"):
        summarize_audited_formal_results(rows, baseline=baseline, manifest=manifest, cell_identities=drifted, reporter_behavior_tree_sha256="r" * 64, reporter_commit="a" * 40, allow_report_only_hotfix=True, confirmed=True)
    audit_runs = [
        {"run_id": row["run_id"], "run_type": "formal_cell", "status": row["status"], "validity": "active", "supersedes": []}
        for row in rows
    ]
    for index in range(3):
        old_id = f"infra-{index}"
        audit_runs.append({"run_id": old_id, "run_type": "formal_cell", "status": "eval_infra_failed", "validity": "active", "supersedes": []})
        active_index = index * 2
        audit_runs[active_index] = {**audit_runs[active_index], "supersedes": [old_id]}
    audit_runs.append({"run_id": "dev-run", "run_type": "dev_cell", "status": "resolved", "validity": "active", "supersedes": []})
    audited = summarize_audited_formal_results(rows, baseline=baseline, manifest=manifest, cell_identities=identities, audit_runs=audit_runs, reporter_behavior_tree_sha256="r" * 64, reporter_commit="a" * 40, allow_report_only_hotfix=True, confirmed=True)
    assert audited["frozen_behavior_tree_sha256"] == "f" * 64
    assert audited["reporter_behavior_tree_sha256"] == "r" * 64
    assert audited["report_only_hotfix"] is True and audited["report_hotfix_commit"] == "e1e8d9a8440d724029f409159f949a8f2457be22"
    assert audited["categories"]["full"]["E1"] == {"count": 12, "total": 12}
    assert audited["E1_resolved"] == report["E1_resolved"] and audited["E2_resolved"] == report["E2_resolved"]
    assert audited["paired_outcomes"] == report["paired_outcomes"]
    assert sum(audited["paired_outcomes"].values()) == 12
    assert audited["infrastructure"] == {
        "infra_failures": 0, "formal_attempts": 27, "active_runs": 24,
        "superseded_runs": 3, "superseded_infra_attempts": 3,
        "supersession_edges": [
            {"run_id": f"run-{index}-full", "supersedes": f"infra-{index}"}
            for index in range(3)
        ],
    }
    assert json.loads(json.dumps(audited, sort_keys=True))["reporter_commit"] == "a" * 40

    incomplete = [dict(row) for row in rows]
    incomplete[0]["status"] = "eval_infra_failed"
    with pytest.raises(EvalError, match="incomplete"):
        summarize_formal_results(incomplete)
    with pytest.raises(EvalError, match="24 cells"):
        summarize_formal_results(rows[:-1])
    duplicate = list(rows)
    duplicate[-1] = dict(rows[0])
    with pytest.raises(EvalError, match="duplicate"):
        summarize_formal_results(duplicate)


def test_invalid_limits_distinguish_consecutive_from_total(tmp_path: Path):
    repo = tmp_path / "limits-repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "x").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "x"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
    raw = json.loads((ROOT / "configs/agent/offline-scripted.json").read_text(encoding="utf-8"))
    raw["budgets"].pop("max_invalid_outputs", None)
    raw["budgets"].update({"max_consecutive_invalid_outputs": 2, "max_total_invalid_outputs": 3, "max_steps": 8})
    invalid = {"text": "no call", "tool_calls": [], "usage": {}, "finish_reason": "stop", "provider_request_id": "invalid"}
    valid = {"text": "", "tool_calls": [{"call_id": "read", "name": "read_file", "arguments": {"path": "x"}}], "usage": {}, "finish_reason": "tool_calls", "provider_request_id": "valid"}
    raw["script"] = [invalid, valid, invalid, valid, invalid]
    path = tmp_path / "limits.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    cfg = AgentConfig.load(path)
    workspace = GitWorkspace.create(repo)
    store = RunStore.create(tmp_path / "runs")
    ledger = ContextLedger("system", "task", context_window=10000, trigger_ratio=.8, keep_recent_rounds=2)
    result = AgentLoop(ScriptedModel(cfg.script), build_registry(workspace, cfg.raw, command_executor=LocalTestCommandExecutor(), artifact_dir=store.path / "commands"), workspace, cfg, ledger, store).run()
    assert result.stop_reason == "invalid_output_limit"
    checkpoint = json.loads((store.path / "checkpoints" / (store.path / "LATEST").read_text().strip()).read_text())
    assert checkpoint["payload"]["invalid_outputs"] == 3
    assert checkpoint["payload"]["consecutive_invalid_outputs"] == 1


def _harness_runner(overrides: dict[str, subprocess.CompletedProcess] | None = None):
    overrides = overrides or {}
    def run(argv, **kwargs):
        key = " ".join(str(item) for item in argv)
        for marker, result in overrides.items():
            if marker in key:
                return result
        if "status --porcelain" in key:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "rev-parse HEAD" in key:
            return subprocess.CompletedProcess(argv, 0, "7a21e05772954cc81471ae19d56f436cecf43c54\n", "")
        if "show HEAD:uv.lock" in key:
            return subprocess.CompletedProcess(argv, 0, "canonical-lock", "")
        if "-c" in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"python":"3.11.16","versions":{"docker":"7.2.0","swebench":"5.0.2","datasets":"5.0.1","GitPython":"3.1.59","tqdm":"4.70.0","unidiff":"1.0.0","rich":"15.0.0","requests":"2.34.2"},"imports":True,"docker_ping":True,"sys_executable":"/cache/swe-bench/.venv/bin/python","sys_prefix":"/cache/swe-bench/.venv","site_packages":["/cache/swe-bench/.venv/lib/python3.11/site-packages"],"no_site":0,"distribution":"Ubuntu","environment":{"PYTHONPATH":"unset","PYTHONHOME":"unset","VIRTUAL_ENV":"unset","WSLENV":"unset"}}), "")
        return subprocess.CompletedProcess(argv, 0, "", "")
    return run


def test_harness_environment_gate_accepts_canonical_and_writes_receipt(tmp_path: Path, monkeypatch):
    checkout = tmp_path / "cache/swe-bench"; checkout.mkdir(parents=True)
    (checkout / "uv.lock").write_text("canonical-lock", encoding="utf-8")
    python = checkout / ".venv/bin/python"; python.parent.mkdir(parents=True); python.write_text("", encoding="utf-8")
    expected = __import__("hashlib").sha256(b"canonical-lock").hexdigest()
    monkeypatch.setattr("evalsys.harness_environment.CANONICAL_LOCK_SHA256", expected)
    receipt = verify_harness_environment(tmp_path, checkout, str(python), expected_head="7a21e05772954cc81471ae19d56f436cecf43c54", expected_lock_sha256=expected, runner=_harness_runner())
    assert receipt["status"] == "passed" and receipt["versions"]["docker"] == "7.2.0"
    reference = receipt["reference"]
    receipt_bytes = (tmp_path / reference["path"]).read_bytes()
    assert b"\r\n" not in receipt_bytes and receipt_bytes.endswith(b"\n") and not receipt_bytes.endswith(b"\n\n")
    assert reference["sha256"] == __import__("hashlib").sha256(receipt_bytes).hexdigest()
    assert verify_harness_environment_receipt(tmp_path, reference)["status"] == "passed"
    (tmp_path / reference["path"]).write_text("{}", encoding="utf-8")
    with pytest.raises(EvalError, match="receipt SHA-256"):
        verify_harness_environment_receipt(tmp_path, reference)


@pytest.mark.parametrize("marker,message", [
    ("status --porcelain", "dirty"),
    ("show HEAD:uv.lock", "lock"),
    ("runtime-preflight", "imports"),
])
def test_harness_environment_gate_rejects_invalid_state(tmp_path: Path, marker: str, message: str):
    checkout = tmp_path / "cache/swe-bench"; checkout.mkdir(parents=True)
    (checkout / "uv.lock").write_text("canonical-lock", encoding="utf-8")
    python = checkout / ".venv/bin/python"; python.parent.mkdir(parents=True); python.write_text("", encoding="utf-8")
    expected = __import__("hashlib").sha256(b"canonical-lock").hexdigest()
    if marker == "status --porcelain":
        result = subprocess.CompletedProcess([], 0, " M file\n", "")
    elif marker == "show HEAD:uv.lock":
        result = subprocess.CompletedProcess([], 0, "different-lock", "")
    else:
        result = subprocess.CompletedProcess([], 1, "", "missing docker")
    with pytest.raises(EvalError, match=message):
        verify_harness_environment(tmp_path, checkout, str(python), expected_head="7a21e05772954cc81471ae19d56f436cecf43c54", expected_lock_sha256=expected, runner=_harness_runner({marker: result}))


def test_harness_environment_uses_explicit_wsl_checkout_path(tmp_path: Path, monkeypatch):
    checkout = tmp_path / "cache/swe-bench"; checkout.mkdir(parents=True)
    (checkout / "uv.lock").write_text("canonical-lock", encoding="utf-8")
    expected = __import__("hashlib").sha256(b"canonical-lock").hexdigest()
    monkeypatch.setattr("evalsys.harness_environment.CANONICAL_LOCK_SHA256", expected)
    calls = []
    base = _harness_runner()
    def runner(argv, **kwargs):
        calls.append(argv)
        return base(argv, **kwargs)
    receipt = verify_harness_environment(tmp_path, checkout, "/mnt/d/cache/swe-bench/.venv/bin/python", expected_head="7a21e05772954cc81471ae19d56f436cecf43c54", expected_lock_sha256=expected, runner=runner, command_prefix=["wsl.exe", "--"], execution_checkout="/mnt/d/cache/swe-bench")
    assert receipt["interpreter"] == ".venv/bin/python"
    assert any("/mnt/d/cache/swe-bench" in call for argv in calls for call in argv)
    assert all(str(checkout) not in call for argv in calls for call in argv)


def test_harness_environment_rejects_interpreter_outside_checkout(tmp_path: Path):
    checkout = tmp_path / "cache/swe-bench"; checkout.mkdir(parents=True)
    (checkout / "uv.lock").write_text("canonical-lock", encoding="utf-8")
    expected = __import__("hashlib").sha256(b"canonical-lock").hexdigest()
    with pytest.raises(EvalError, match="interpreter"):
        verify_harness_environment(tmp_path, checkout, "/other/python", expected_head="7a21e05772954cc81471ae19d56f436cecf43c54", expected_lock_sha256=expected, runner=_harness_runner())


def test_git_gate_rejects_dirty_unpushed_and_hash_mismatch(tmp_path: Path):
    repo = tmp_path / "gate"
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(remote)], check=True)
    (repo / "x").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "x"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
    subprocess.run(["git", "-C", str(repo), "push", "-q", "-u", "origin", "HEAD:main"], check=True)
    head = verify_git_gate(repo)
    assert len(head) == 40
    (repo / "x").write_text("dirty", encoding="utf-8")
    with pytest.raises(EvalError, match="dirty"):
        verify_git_gate(repo)
    subprocess.run(["git", "-C", str(repo), "restore", "x"], check=True)
    (repo / "y").write_text("y", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "y"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "unpushed"], check=True)
    with pytest.raises(EvalError, match="origin/main"):
        verify_git_gate(repo)


def _valid_development_record(config: dict | None = None) -> dict:
    cells = []
    for index, record in enumerate(development_cells(_records())):
        cells.append({
            "case_id": record["case_id"], "instance_id": record["instance_id"],
            "variant": record["prompt_variant"], "run_id": f"run-{index}",
            "status": "unresolved", "checksum": "a" * 64,
            "evaluator_recorded": True, "actual_model": "gpt-5.6-sol",
        })
    return {
        "source_run_ids": [cell["run_id"] for cell in cells], "cells": cells,
        "config_hash": __import__("hashlib").sha256(json.dumps(config or {}, sort_keys=True, separators=(",", ":")).encode()).hexdigest(), "system_prompt_hash": "c" * 64,
        "protocol_prompt_hash": "d" * 64, "tool_schema_hash": "e" * 64,
        "code_commit": "f" * 40, "code_hash": "1" * 64,
        "test_receipt": {"path": "audit/iteration2/test-receipt.json", "sha256": "2" * 64},
        "isolation_proof": {"path": "audit/iteration2/isolation-proof.json", "sha256": "3" * 64},
        "provider_identity": {"actual_model": "gpt-5.6-sol"},
        "harness_environment": {"path": "audit/iteration2/harness-environment-receipt.json", "sha256": "4" * 64},
        "scheduler": "deterministic_wave_v1", "parallel_cells": 2,
        "pair_order_source": "frozen_plan", "result_order": "frozen_plan_sequence",
    }


def _valid_images() -> dict:
    ids = {record["instance_id"] for record in development_cells(_records())} | set(FORMAL_INSTANCES)
    return {instance_id: {"available": True, "image_id": "sha256:" + "a" * 64, "repo_digests": [f"swebench/{instance_id}@sha256:" + "b" * 64]} for instance_id in ids}


def _write_gate_file(project: Path, name: str, bindings: dict) -> dict:
    path = project / "audit/iteration2" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": "1.0", "status": "passed", **bindings}
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return {"path": f"audit/iteration2/{name}", "sha256": __import__("hashlib").sha256(path.read_bytes()).hexdigest()}


def test_development_evidence_revalidates_raw_checksum_and_active_leaf(tmp_path: Path):
    project = tmp_path / "project"
    raw_root = project / "artifacts/runs/iteration2"
    audit = project / "audit/iteration2"
    audit.mkdir(parents=True)
    cells = []
    index = {"schema_version": 1, "runs": []}
    for position, public in enumerate(development_cells(_records())):
        run_id = f"run-{position}"
        raw = raw_root / run_id
        raw.mkdir(parents=True)
        cell = {"run_id": run_id, "case_id": public["case_id"], "instance_id": public["instance_id"], "variant": public["prompt_variant"], "status": "unresolved", "evaluator_recorded": True}
        path = raw / "cell-result.json"
        path.write_text(json.dumps(cell, sort_keys=True), encoding="utf-8")
        checksum = __import__("hashlib").sha256(path.read_bytes()).hexdigest()
        (raw / "checksums.sha256").write_text(f"{checksum}  cell-result.json\n", encoding="utf-8")
        (raw / "COMPLETE").write_text("complete\n", encoding="utf-8")
        index["runs"].append({"run_id": run_id, "run_type": "dev_cell", "status": "unresolved", "config_hash": "cfg", "raw_path": f"artifacts/runs/iteration2/{run_id}", "audit_path": f"audit/iteration2/runs/{run_id}", "validity": "active", "supersedes": []})
        cells.append({**cell, "checksum": checksum})
    (audit / "index.json").write_text(json.dumps(index), encoding="utf-8")
    verified = verify_development_evidence(project, raw_root, cells)
    assert [cell["checksum"] for cell in verified] == [cell["checksum"] for cell in cells]
    (raw_root / "run-0/cell-result.json").write_text("{}", encoding="utf-8")
    with pytest.raises(EvalError, match="checksum"):
        verify_development_evidence(project, raw_root, cells)


def test_development_record_comparison_accepts_sanitized_usage_but_rejects_identity_drift():
    raw = {"run_id": "run-1", "case_id": "D-O1-full", "instance_id": "django__django-11133", "variant": "full", "status": "resolved", "evaluator_recorded": True, "actual_model": "gpt-5.6-sol", "checksum": "a" * 64, "usage": {"input_tokens": 10}}
    public = {**raw, "usage": {"input_tokens": "[REDACTED]"}}
    verify_development_record_cells([public], [raw])
    with pytest.raises(EvalError, match="identity"):
        verify_development_record_cells([{**public, "status": "unresolved"}], [raw])


def test_gate_receipt_reloads_path_hash_status_and_bindings(tmp_path: Path):
    project = tmp_path / "project"
    bindings = {"behavior_tree_sha256": "a" * 64, "config_hash": "b" * 64, "system_prompt_hash": "c" * 64, "protocol_prompt_hash": "d" * 64, "tool_schema_hash": "e" * 64}
    reference = _write_gate_file(project, "test-receipt.json", bindings)
    assert verify_gate_receipt(project, reference, bindings, label="test receipt")["status"] == "passed"
    (project / reference["path"]).write_text("{}", encoding="utf-8")
    with pytest.raises(EvalError, match="SHA-256"):
        verify_gate_receipt(project, reference, bindings, label="test receipt")


def test_test_receipt_writes_fixed_sanitized_binding(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    bindings = {"behavior_tree_sha256": "a" * 64, "config_hash": "b" * 64, "system_prompt_hash": "c" * 64, "protocol_prompt_hash": "d" * 64, "tool_schema_hash": "e" * 64}
    reference = write_test_receipt(project, bindings, command="pytest -q", exit_code=0, counts={"passed": 270, "skipped": 5, "deselected": 1})
    assert reference["path"] == "audit/iteration2/test-receipt.json"
    receipt_bytes = (project / reference["path"]).read_bytes()
    assert b"\r\n" not in receipt_bytes and receipt_bytes.endswith(b"\n") and not receipt_bytes.endswith(b"\n\n")
    assert reference["sha256"] == __import__("hashlib").sha256(receipt_bytes).hexdigest()
    payload = verify_gate_receipt(project, reference, bindings, label="test receipt")
    assert payload["counts"] == {"passed": 270, "skipped": 5, "deselected": 1}


def test_isolation_diagnostic_writes_failure_matrix_without_canonical_proof(tmp_path: Path):
    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    project.mkdir(); workspace.mkdir()
    def runner(argv, **kwargs):
        if "ps" in argv:
            return subprocess.CompletedProcess(argv, 0, "", "")
        payload = {"container_started": True, "workspace_read": False, "workspace_write": True, "network_absent": True, "credentials_absent": True, "docker_socket_absent": True, "private_paths_absent": True, "host_paths_absent": True, "python_command": True, "git_command": True, "mount_inventory": [{"target": "/workspace", "readonly": False}]}
        return subprocess.CompletedProcess(argv, 7, json.dumps(payload), "read failed at /workspace")
    result = run_isolation_diagnostic(project, workspace, image="repo/task@sha256:" + "f" * 64, image_identity={"image_id": "sha256:" + "e" * 64, "repo_digests": ["repo/task@sha256:" + "f" * 64]}, docker_prefix=["docker"], runner=runner, path_converter=str, run_id="diag-1")
    assert result["status"] == "failed"
    assert result["failed_probes"] == ["workspace_read"]
    assert "read failed" in result["failure_reason"]
    assert not (project / "audit/iteration2/isolation-proof.json").exists()
    receipt = project / "audit/iteration2/isolation-failures/diag-1.json"
    assert receipt.is_file()
    text = receipt.read_text(encoding="utf-8")
    assert str(workspace) not in text


def test_utf8_bytes_paths_detect_presence_without_unicode_encoding(tmp_path: Path):
    import os
    root = os.fsencode(str(tmp_path))
    plan_name = b"\xe8\xae\xa1\xe5\x88\x92"
    materials_name = b"\xe8\xb5\x84\xe6\x96\x99"
    os.mkdir(os.path.join(root, plan_name))
    assert os.path.lexists(os.path.join(root, plan_name))
    assert not os.path.lexists(os.path.join(root, materials_name))


def test_isolation_probe_exception_remains_unknown(tmp_path: Path):
    project = tmp_path / "project"; workspace = tmp_path / "workspace"
    project.mkdir(); workspace.mkdir()
    def runner(argv, **kwargs):
        if "ps" in argv:
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 1, "", "probe exception")
    result = run_isolation_diagnostic(project, workspace, image="repo/task@sha256:" + "f" * 64, image_identity={"image_id": "sha256:" + "e" * 64, "repo_digests": ["repo/task@sha256:" + "f" * 64]}, docker_prefix=["docker"], runner=runner, path_converter=str, run_id="unknown-1")
    assert result["status"] == "failed"
    assert all(row["actual"] is None for row in result["matrix"] if row["probe"] not in {"cleanup", "mount_inventory"})
    assert not (project / "audit/iteration2/isolation-proof.json").exists()


def test_isolation_proof_uses_one_hardened_workspace_mount(tmp_path: Path):
    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    project.mkdir(); workspace.mkdir()
    bindings = {"behavior_tree_sha256": "a" * 64, "config_hash": "b" * 64, "system_prompt_hash": "c" * 64, "protocol_prompt_hash": "d" * 64, "tool_schema_hash": "e" * 64}
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        if "ps" in argv:
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"container_started": True, "workspace_read": True, "workspace_write": True, "python_command": True, "git_command": True, "network_absent": True, "credentials_absent": True, "docker_socket_absent": True, "private_paths_absent": True, "host_paths_absent": True, "mount_inventory": [{"target": "/workspace", "readonly": False}]}), "")
    reference = write_isolation_proof(project, workspace, bindings, image="repo/task@sha256:" + "f" * 64, docker_prefix=["docker"], runner=runner, path_converter=str)
    command = calls[0]
    command[-1].encode("ascii")
    assert "计划" not in command[-1] and "资料" not in command[-1]
    assert "capture_output" not in command[-1]
    assert "stdout=subprocess.PIPE" in command[-1]
    assert "os.path.lexists" in command[-1]
    assert "b'\\xe8\\xae\\xa1\\xe5\\x88\\x92'" in command[-1]
    assert "b'\\xe8\\xb5\\x84\\xe6\\x96\\x99'" in command[-1]
    assert command[command.index("--pull") + 1] == "never"
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert [command[index + 1] for index, value in enumerate(command) if value == "--mount"] == [f"type=bind,src={workspace.resolve()},dst=/workspace"]
    verify_gate_receipt(project, reference, bindings, label="isolation proof")


def test_runtime_provider_rejects_endpoint_or_actual_model_drift(monkeypatch):
    endpoint = "https://example.invalid/proxy"
    monkeypatch.setenv("ANTHROPIC_BASE_URL", endpoint)
    identity = {"endpoint_sha256": __import__("hashlib").sha256(endpoint.encode()).hexdigest(), "actual_model": "gpt-5.6-sol", "base_url_env": "ANTHROPIC_BASE_URL", "api_key_env": "ANTHROPIC_AUTH_TOKEN"}
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "placeholder-token")
    verify_runtime_provider(identity)
    with pytest.raises(EvalError, match="actual model"):
        verify_runtime_provider(identity, actual_model="different-model")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", endpoint + "/changed")
    with pytest.raises(EvalError, match="endpoint"):
        verify_runtime_provider(identity)


def test_formal_loader_revalidates_plan_manifest_raw_and_audit(tmp_path: Path):
    project = tmp_path / "project"
    raw_root = project / "artifacts/runs/iteration2"
    formal = raw_root / "formal/baseline-v1"
    formal.mkdir(parents=True)
    recorder = EvidenceRecorder(project, iteration=2, raw_root=raw_root)
    plan = []
    cell_runs = {}
    for position in range(24):
        variant = "full" if position % 2 == 0 else "fuzzy"
        pair = position // 2
        case_id = f"T-{pair}-{variant}"
        plan.append({"sequence": position + 1, "case_id": case_id, "instance_id": f"instance-{pair}", "variant": variant, "ambiguity_type": "omission"})
        run = recorder.start("formal_cell", {"case_id": case_id}, ["formal"])
        cell = {"run_id": run.run_id, "case_id": case_id, "instance_id": f"instance-{pair}", "variant": variant, "ambiguity_type": "omission", "status": "unresolved", "evaluator_recorded": True, "usage": {}, "steps": 1, "tool_calls": 1, "wall_time_seconds": 1, "patch": {"files": 0, "additions": 0, "deletions": 0, "bytes": 0}, "stop_reason": "submitted"}
        (run.raw_dir / "cell-result.json").write_text(json.dumps(cell, sort_keys=True), encoding="utf-8")
        run.finish({"status": "unresolved", "classification": "tests_failed"})
        cell_runs[case_id] = {"run_id": run.run_id, "state": "complete"}
    (formal / "experiment-manifest.json").write_text(json.dumps({"cell_runs": cell_runs}), encoding="utf-8")
    rows = load_formal_results(project, raw_root, plan, formal / "experiment-manifest.json")
    assert len(rows) == 24
    first = raw_root / rows[0]["run_id"] / "cell-result.json"
    first.write_text("{}", encoding="utf-8")
    with pytest.raises(EvalError, match="checksum"):
        load_formal_results(project, raw_root, plan, formal / "experiment-manifest.json")


def test_provider_identity_uses_only_endpoint_hash_and_verified_actual_model(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    summary = project / "audit/iteration2/runs/capability/summary.json"
    summary.parent.mkdir(parents=True)
    endpoint_hash = __import__("hashlib").sha256(b"https://example.invalid/proxy").hexdigest()
    summary.write_text(json.dumps({"status": "passed", "provider": "local_reverse_proxy", "protocol": "anthropic_messages", "configured_model": "gpt-5.6-sol", "actual_model": "gpt-5.6-sol", "environment_variables": ["ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"], "endpoint_fingerprint_sha256": endpoint_hash, "temperature": "unsupported", "seed": "unsupported", "native_tool_calling": "passed", "context_window_tokens": {"value": 32768}, "max_output_tokens": 4096}), encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://example.invalid/proxy")
    identity = load_provider_identity(project, summary.relative_to(project).as_posix(), expected_endpoint_sha256=endpoint_hash)
    assert identity["actual_model"] == "gpt-5.6-sol"
    assert "endpoint" not in identity and identity["base_url_env"] == "ANTHROPIC_BASE_URL"


@pytest.mark.parametrize("interrupt_point", ["after_model_checkpoint", "after_tool_checkpoint"])
def test_run_development_resumes_same_pending_run(tmp_path: Path, monkeypatch, interrupt_point: str):
    from reqagent.loop import AgentInterrupted
    project = tmp_path / "project"
    project.mkdir(); (tmp_path / "cache").mkdir()
    settings = Settings(project, tmp_path / "cache", project / "artifacts")
    (project / "benchmark/manifests").mkdir(parents=True)
    (project / "benchmark/manifests/paired-cases.jsonl").write_text("".join(json.dumps(row) + "\n" for row in _records()), encoding="utf-8")
    (project / "prompts/baseline").mkdir(parents=True)
    (project / "prompts/baseline/system.txt").write_text("system\n", encoding="utf-8")
    (project / "prompts/baseline/protocol.txt").write_text("protocol\n", encoding="utf-8")
    config_raw = json.loads((ROOT / "configs/agent/live-local-proxy.json").read_text(encoding="utf-8"))
    config_path = tmp_path / "config.json"; config_path.write_text(json.dumps(config_raw), encoding="utf-8")
    config = AgentConfig.load(config_path)
    monkeypatch.setattr("evalsys.iteration2.current_tool_schema_bytes", lambda root, value: b"schema\n")
    bindings = {"behavior_tree_sha256": behavior_tree_hash(project), "config_hash": config.canonical_hash(), "system_prompt_hash": __import__("hashlib").sha256((project / "prompts/baseline/system.txt").read_bytes()).hexdigest(), "protocol_prompt_hash": __import__("hashlib").sha256((project / "prompts/baseline/protocol.txt").read_bytes()).hexdigest(), "tool_schema_hash": __import__("hashlib").sha256(b"schema\n").hexdigest()}
    test_ref = _write_gate_file(project, "test-receipt.json", bindings); isolation_ref = _write_gate_file(project, "isolation-proof.json", bindings)
    provider = {"actual_model": "gpt-5.6-sol"}
    calls = []
    def interrupted(settings, case, source_row, config, **kwargs):
        calls.append((interrupt_point, kwargs.get("resume_run_id")))
        kwargs["on_started"]("pending-run")
        raise AgentInterrupted(interrupt_point)
    monkeypatch.setattr("evalsys.iteration2.run_agent_cell", interrupted)
    with pytest.raises(AgentInterrupted):
        run_development(settings, "v001", config, {row["instance_id"]: {} for row in _records()}, _valid_images(), resume=False, test_receipt=test_ref, isolation_proof=isolation_ref, provider_identity=provider)
    manifest = json.loads((project / "artifacts/runs/iteration2/dev/v001/experiment-manifest.json").read_text(encoding="utf-8"))
    assert manifest["cell_runs"]["D-O1-full"] == {"run_id": "pending-run", "state": "pending"}
    def resumed(settings, case, source_row, config, **kwargs):
        assert kwargs["resume_run_id"] == "pending-run"
        raise RuntimeError("resume reached same pending run")
    monkeypatch.setattr("evalsys.iteration2.run_agent_cell", resumed)
    with pytest.raises(RuntimeError, match="same pending run"):
        run_development(settings, "v001", config, {row["instance_id"]: {} for row in _records()}, _valid_images(), resume=True, test_receipt=test_ref, isolation_proof=isolation_ref, provider_identity=provider)


def test_run_development_output_freezes_without_manual_fields(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    settings = Settings(project, cache, project / "artifacts")
    (project / "benchmark/manifests").mkdir(parents=True)
    (project / "benchmark/manifests/paired-cases.jsonl").write_text("".join(json.dumps(row) + "\n" for row in _records()), encoding="utf-8")
    (project / "benchmark/source-lock.json").write_text("{}\n", encoding="utf-8")
    (project / "prompts/baseline").mkdir(parents=True)
    (project / "prompts/baseline/system.txt").write_text("system\n", encoding="utf-8")
    (project / "prompts/baseline/protocol.txt").write_text("protocol\n", encoding="utf-8")
    (project / "src/evalsys").mkdir(parents=True)
    (project / "src/evalsys/baseline.py").write_text("generator\n", encoding="utf-8")
    (project / "uv.lock").write_text("lock\n", encoding="utf-8")
    config_raw = json.loads((ROOT / "configs/agent/live-local-proxy.json").read_text(encoding="utf-8"))
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config_raw), encoding="utf-8")
    config = AgentConfig.load(config_path)
    bindings = {"behavior_tree_sha256": behavior_tree_hash(project), "config_hash": config.canonical_hash(), "system_prompt_hash": __import__("hashlib").sha256((project / "prompts/baseline/system.txt").read_bytes()).hexdigest(), "protocol_prompt_hash": __import__("hashlib").sha256((project / "prompts/baseline/protocol.txt").read_bytes()).hexdigest(), "tool_schema_hash": "e" * 64}
    monkeypatch.setattr("evalsys.iteration2.current_tool_schema_bytes", lambda root, value: b"schema\n")
    bindings["tool_schema_hash"] = __import__("hashlib").sha256(b"schema\n").hexdigest()
    test_ref = _write_gate_file(project, "test-receipt.json", bindings)
    isolation_ref = _write_gate_file(project, "isolation-proof.json", bindings)
    harness_path = project / "audit/iteration2/harness-environment-receipt.json"
    harness_path.write_text(json.dumps({"status": "passed", "canonical_lock_sha256": "66ada0bfcc5177def68d5307e0c6fdaf5b91b5659258faa1fb2cc4862809d39e"}), encoding="utf-8")
    harness_ref = {"path": harness_path.relative_to(project).as_posix(), "sha256": __import__("hashlib").sha256(harness_path.read_bytes()).hexdigest()}
    endpoint = "https://placeholder.invalid/proxy"
    monkeypatch.setenv("ANTHROPIC_BASE_URL", endpoint)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "placeholder-token")
    provider = {"actual_model": "gpt-5.6-sol", "endpoint_sha256": __import__("hashlib").sha256(endpoint.encode()).hexdigest(), "base_url_env": "ANTHROPIC_BASE_URL", "api_key_env": "ANTHROPIC_AUTH_TOKEN"}
    recorder = EvidenceRecorder(project, iteration=2, raw_root=project / "artifacts/runs/iteration2")
    counter = iter(range(6))
    active = maximum = 0
    active_lock = __import__("threading").Lock()
    def fake_cell(settings, case, source_row, config, **kwargs):
        nonlocal active, maximum
        position = next(counter)
        with active_lock:
            active += 1
            maximum = max(maximum, active)
        __import__("time").sleep(0.03)
        run = recorder.start("dev_cell", {"cell": case["case_id"]}, ["fake-cell"])
        cell = {"run_id": run.run_id, "case_id": case["case_id"], "instance_id": case["instance_id"], "variant": case["prompt_variant"], "status": "unresolved", "evaluator_recorded": True, "actual_model": "gpt-5.6-sol", "usage": {}, "steps": 1, "tool_calls": 1, "wall_time_seconds": 1, "patch": {"files": 0, "additions": 0, "deletions": 0, "bytes": 0}, "stop_reason": "submitted"}
        (run.raw_dir / "cell-result.json").write_text(json.dumps(cell, sort_keys=True), encoding="utf-8")
        run.finish({"status": "unresolved", "classification": "tests_failed"})
        with active_lock:
            active -= 1
        return cell
    monkeypatch.setattr("evalsys.iteration2.run_agent_cell", fake_cell)
    monkeypatch.setattr("evalsys.iteration2._git", lambda root, *args: "f" * 40)
    first = run_development(settings, "v001", config, {row["instance_id"]: {} for row in _records()}, _valid_images(), resume=False, test_receipt=test_ref, isolation_proof=isolation_ref, provider_identity=provider, harness_environment=harness_ref, max_new_cells=4, parallel_cells=2)
    assert first["status"] == "paused" and first["completed"] == 4 and first["new_cells"] == 4
    assert maximum == 2
    first_cases = [cell["case_id"] for cell in first["cells"]]
    assert first_cases == ["D-O1-full", "D-O1-fuzzy", "D-S1-full", "D-S1-fuzzy"]
    development = run_development(settings, "v001", config, {row["instance_id"]: {} for row in _records()}, _valid_images(), resume=True, test_receipt=test_ref, isolation_proof=isolation_ref, provider_identity=provider, harness_environment=harness_ref, max_new_cells=2, parallel_cells=2)
    assert all(len(cell["checksum"]) == 64 for cell in development["cells"])
    assert development["scheduler"] == "deterministic_wave_v1" and development["parallel_cells"] == 2
    assert development["system_prompt_hash"] == bindings["system_prompt_hash"]
    frozen = freeze_baseline(project, "baseline-v1", config.raw, _records(), development=development, image_identities=_valid_images(), authorized=True, git_commit="f" * 40, artifact_root=project / "artifacts/runs/iteration2")
    assert (frozen / "baseline.json").is_file()
    first = project / "artifacts/runs/iteration2" / development["source_run_ids"][0] / "cell-result.json"
    first.write_text("{}", encoding="utf-8")
    with pytest.raises(EvalError, match="checksum"):
        freeze_baseline(project, "baseline-v2", config.raw, _records(), development=development, image_identities=_valid_images(), authorized=True, git_commit="f" * 40, artifact_root=project / "artifacts/runs/iteration2")


def test_freeze_requires_authorization_development_and_image_identities(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("evalsys.iteration2.verify_development_evidence", lambda root, artifacts, cells: cells)
    monkeypatch.setattr("evalsys.iteration2.verify_gate_receipt", lambda root, reference, bindings, label: {"status": "passed"})
    monkeypatch.setattr("evalsys.iteration2.verify_runtime_provider", lambda identity, actual_model=None: None)
    monkeypatch.setattr("evalsys.harness_environment.verify_harness_environment_receipt", lambda root, reference: {"status": "passed"})
    records = [record for record in _records() if record["split"] == "test"]
    config = json.loads((ROOT / "configs/agent/live-local-proxy.json").read_text(encoding="utf-8"))
    config["budgets"].pop("max_invalid_outputs", None)
    config["budgets"].update({"max_consecutive_invalid_outputs": 3, "max_total_invalid_outputs": 6, "max_steps": 30, "max_tool_calls": 60, "wall_clock_seconds": 1800, "command_timeout_seconds": 300, "max_retries": 3})
    with pytest.raises(EvalError, match="authorization"):
        freeze_baseline(tmp_path, "baseline-v1", config, records, development=None, image_identities={}, authorized=False, git_commit="a" * 40)
    with pytest.raises(EvalError, match="development"):
        freeze_baseline(tmp_path, "baseline-v1", config, records, development=None, image_identities={}, authorized=True, git_commit="a" * 40)
    development = _valid_development_record(config)
    missing = _valid_images()
    missing[next(iter(missing))] = {"available": False, "image_id": None}
    with pytest.raises(EvalError, match="must be resolved"):
        freeze_baseline(tmp_path, "baseline-v1", config, records, development=development, image_identities=missing, authorized=True, git_commit="a" * 40)
    fake_keys = {f"fake-{index}": {"available": True, "image_id": "sha256:" + "a" * 64, "repo_digests": ["fake@sha256:" + "b" * 64]} for index in range(15)}
    with pytest.raises(EvalError, match="image inventory keys"):
        freeze_baseline(tmp_path, "baseline-v1", config, records, development=development, image_identities=fake_keys, authorized=True, git_commit="a" * 40)
    invalid = _valid_development_record(config)
    invalid["cells"][0]["status"] = "eval_infra_failed"
    with pytest.raises(EvalError, match="development cell"):
        freeze_baseline(tmp_path, "baseline-v1", config, records, development=invalid, image_identities=_valid_images(), authorized=True, git_commit="a" * 40)
    missing_receipts = _valid_development_record(config)
    missing_receipts.pop("test_receipt")
    with pytest.raises(EvalError, match="test receipt"):
        freeze_baseline(tmp_path, "baseline-v1", config, records, development=missing_receipts, image_identities=_valid_images(), authorized=True, git_commit="a" * 40)


def test_freeze_writes_prompt_schema_and_lock_hashes(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("evalsys.iteration2.verify_development_evidence", lambda root, artifacts, cells: cells)
    monkeypatch.setattr("evalsys.iteration2.verify_gate_receipt", lambda root, reference, bindings, label: {"status": "passed"})
    monkeypatch.setattr("evalsys.iteration2.verify_runtime_provider", lambda identity, actual_model=None: None)
    monkeypatch.setattr("evalsys.harness_environment.verify_harness_environment_receipt", lambda root, reference: {"status": "passed"})
    (tmp_path / "prompts/baseline").mkdir(parents=True)
    (tmp_path / "prompts/baseline/system.txt").write_text("system\n", encoding="utf-8")
    (tmp_path / "prompts/baseline/protocol.txt").write_text("protocol\n", encoding="utf-8")
    (tmp_path / "benchmark/manifests").mkdir(parents=True)
    (tmp_path / "benchmark/manifests/paired-cases.jsonl").write_text("manifest\n", encoding="utf-8")
    (tmp_path / "benchmark/source-lock.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("lock\n", encoding="utf-8")
    (tmp_path / "src/evalsys").mkdir(parents=True)
    (tmp_path / "src/evalsys/baseline.py").write_text("generator\n", encoding="utf-8")
    monkeypatch.setattr("evalsys.iteration2._tool_schemas", lambda root, config: [{"name": "tool"}])
    config = json.loads((ROOT / "configs/agent/live-local-proxy.json").read_text(encoding="utf-8"))
    development = _valid_development_record(config)
    schema_bytes = current_tool_schema_bytes(tmp_path, config)
    development.update({
        "system_prompt_hash": __import__("hashlib").sha256((tmp_path / "prompts/baseline/system.txt").read_bytes()).hexdigest(),
        "protocol_prompt_hash": __import__("hashlib").sha256((tmp_path / "prompts/baseline/protocol.txt").read_bytes()).hexdigest(),
        "tool_schema_hash": __import__("hashlib").sha256(schema_bytes).hexdigest(),
        "code_hash": behavior_tree_hash(tmp_path),
    })
    images = _valid_images()
    root = freeze_baseline(tmp_path, "baseline-v1", config, [record for record in _records() if record["split"] == "test"], development=development, image_identities=images, authorized=True, git_commit="a" * 40)
    manifest = json.loads((root / "baseline.json").read_text(encoding="utf-8"))
    assert manifest["provider_hard_context_limit"] == "unavailable"
    assert manifest["scheduler"] == "deterministic_wave_v1"
    assert manifest["parallel_cells"] == 2
    assert manifest["pair_order_source"] == "frozen_plan"
    assert manifest["result_order"] == "frozen_plan_sequence"
    assert manifest["authorization"]["kind"] == "conditional_pre_authorization"
    assert {"system.txt", "protocol.txt", "tool-schemas.json"}.issubset(path.name for path in root.iterdir())
    verify_frozen_baseline(root)


def test_verify_frozen_baseline_requires_all_snapshots_and_detects_hash_mismatch(tmp_path: Path):
    root = tmp_path / "baseline"
    root.mkdir()
    files = {"baseline.json": json.dumps({"scheduler": "deterministic_wave_v1", "parallel_cells": 2, "pair_order_source": "frozen_plan", "result_order": "frozen_plan_sequence"}) + "\n", "plan.json": "[]\n"}
    for name, content in files.items():
        (root / name).write_text(content, encoding="utf-8")
    hashes = {name: __import__("hashlib").sha256((root / name).read_bytes()).hexdigest() for name in files}
    (root / "checksums.sha256").write_text("".join(f"{digest}  {name}\n" for name, digest in hashes.items()), encoding="utf-8")
    with pytest.raises(EvalError, match="checksum paths"):
        verify_frozen_baseline(root)
    for name in ("system.txt", "protocol.txt", "tool-schemas.json"):
        (root / name).write_text(name + "\n", encoding="utf-8")
    hashes = {name: __import__("hashlib").sha256(path.read_bytes()).hexdigest() for name in files | {"system.txt": "", "protocol.txt": "", "tool-schemas.json": ""} if (path := root / name)}
    (root / "checksums.sha256").write_text("".join(f"{digest}  {name}\n" for name, digest in hashes.items()), encoding="utf-8")
    verify_frozen_baseline(root)
    (root / "plan.json").write_text("[1]\n", encoding="utf-8")
    with pytest.raises(EvalError, match="checksum"):
        verify_frozen_baseline(root)


def test_frozen_baseline_rejects_scheduler_drift(tmp_path: Path):
    root = tmp_path / "baseline"
    root.mkdir()
    baseline = {"scheduler": "other", "parallel_cells": 2, "pair_order_source": "frozen_plan", "result_order": "frozen_plan_sequence"}
    contents = {"baseline.json": json.dumps(baseline) + "\n", "plan.json": "[]\n", "system.txt": "s\n", "protocol.txt": "p\n", "tool-schemas.json": "[]\n"}
    for name, content in contents.items():
        (root / name).write_text(content, encoding="utf-8")
    hashes = {name: __import__("hashlib").sha256((root / name).read_bytes()).hexdigest() for name in contents}
    (root / "checksums.sha256").write_text("".join(f"{digest}  {name}\n" for name, digest in sorted(hashes.items())), encoding="utf-8")
    with pytest.raises(EvalError, match="scheduler"):
        verify_frozen_baseline(root)


def test_current_tool_schema_is_canonical_json(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("evalsys.iteration2._tool_schemas", lambda root, config: [{"name": "z"}, {"name": "a"}])
    assert current_tool_schema_bytes(tmp_path, {}) == b'[\n  {\n    "name": "z"\n  },\n  {\n    "name": "a"\n  }\n]\n'


def test_behavior_tree_hash_changes_for_behavior_source(tmp_path: Path):
    (tmp_path / "src/evalsys").mkdir(parents=True)
    (tmp_path / "src/reqagent").mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "src/evalsys/a.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "src/reqagent/b.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "scripts/official_harness_adapter.py").write_text("VALUE = 1\n", encoding="utf-8")
    before = behavior_tree_hash(tmp_path)
    (tmp_path / "src/reqagent/b.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert behavior_tree_hash(tmp_path) != before


def test_frozen_image_identity_drift_is_rejected(tmp_path: Path):
    root = tmp_path / "baseline"
    root.mkdir()
    baseline = {"image_identities": {"case": {"image_id": "sha256:" + "a" * 64, "repo_digests": ["repo@sha256:" + "b" * 64]}}, "scheduler": "deterministic_wave_v1", "parallel_cells": 2, "pair_order_source": "frozen_plan", "result_order": "frozen_plan_sequence"}
    for name, content in {"baseline.json": json.dumps(baseline), "plan.json": "[]", "system.txt": "s", "protocol.txt": "p", "tool-schemas.json": "[]"}.items():
        (root / name).write_text(content + "\n", encoding="utf-8")
    hashes = {path.name: __import__("hashlib").sha256(path.read_bytes()).hexdigest() for path in root.iterdir()}
    (root / "checksums.sha256").write_text("".join(f"{digest}  {name}\n" for name, digest in sorted(hashes.items())), encoding="utf-8")
    with pytest.raises(EvalError, match="image identity drift"):
        verify_frozen_baseline(root, image_resolver=lambda instance_id: {"image_id": "sha256:" + "c" * 64, "repo_digests": ["repo@sha256:" + "d" * 64]})


def test_current_behavior_manifest_prompt_and_lock_drift_are_rejected(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    for directory in ("src/evalsys", "src/reqagent", "scripts", "benchmark/manifests", "benchmark", "prompts/baseline"):
        (project / directory).mkdir(parents=True, exist_ok=True)
    files = {
        "src/evalsys/baseline.py": "generator\n", "src/reqagent/core.py": "code\n",
        "scripts/official_harness_adapter.py": "adapter\n",
        "benchmark/manifests/paired-cases.jsonl": "manifest\n", "benchmark/source-lock.json": "{}\n",
        "uv.lock": "lock\n", "prompts/baseline/system.txt": "system\n", "prompts/baseline/protocol.txt": "protocol\n",
    }
    for name, content in files.items():
        (project / name).write_text(content, encoding="utf-8")
    frozen = tmp_path / "frozen"
    frozen.mkdir()
    system = project / "prompts/baseline/system.txt"
    protocol = project / "prompts/baseline/protocol.txt"
    config = json.loads((ROOT / "configs/agent/live-local-proxy.json").read_text(encoding="utf-8"))
    monkeypatch.setattr("evalsys.iteration2._tool_schemas", lambda root, value: [])
    tool_schema = current_tool_schema_bytes(project, config)
    manifest = {
        "public_manifest_sha256": __import__("hashlib").sha256((project / "benchmark/manifests/paired-cases.jsonl").read_bytes()).hexdigest(),
        "source_lock_sha256": __import__("hashlib").sha256((project / "benchmark/source-lock.json").read_bytes()).hexdigest(),
        "dependency_lock_sha256": __import__("hashlib").sha256((project / "uv.lock").read_bytes()).hexdigest(),
        "plan_generator_sha256": __import__("hashlib").sha256((project / "src/evalsys/baseline.py").read_bytes()).hexdigest(),
        "behavior_tree_sha256": behavior_tree_hash(project),
        "system_prompt_sha256": __import__("hashlib").sha256(system.read_bytes()).hexdigest(),
        "protocol_prompt_sha256": __import__("hashlib").sha256(protocol.read_bytes()).hexdigest(),
        "tool_schema_sha256": __import__("hashlib").sha256(tool_schema).hexdigest(),
        "config": config,
        "config_sha256": __import__("hashlib").sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "scheduler": "deterministic_wave_v1", "parallel_cells": 2,
        "pair_order_source": "frozen_plan", "result_order": "frozen_plan_sequence",
    }
    contents = {"baseline.json": (json.dumps(manifest) + "\n").encode(), "plan.json": b"[]\n", "system.txt": system.read_bytes(), "protocol.txt": protocol.read_bytes(), "tool-schemas.json": tool_schema}
    for name, content in contents.items():
        (frozen / name).write_bytes(content)
    hashes = {name: __import__("hashlib").sha256((frozen / name).read_bytes()).hexdigest() for name in contents}
    (frozen / "checksums.sha256").write_text("".join(f"{digest}  {name}\n" for name, digest in sorted(hashes.items())), encoding="utf-8")
    verify_frozen_baseline(frozen, project_root=project)
    (project / "src/reqagent/core.py").write_text("changed\n", encoding="utf-8")
    with pytest.raises(EvalError, match="behavior_tree"):
        verify_frozen_baseline(frozen, project_root=project)


def test_execute_cleans_normal_temporary_clone(tmp_path: Path):
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "x").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "x"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
    raw = json.loads((ROOT / "configs/agent/offline-scripted.json").read_text(encoding="utf-8"))
    raw["script"] = [{"text": "", "tool_calls": [{"call_id": "submit", "name": "submit", "arguments": {"summary": "done", "tests": [], "limitations": ""}}], "usage": {}, "finish_reason": "tool_calls", "provider_request_id": "one"}]
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = AgentConfig.load(config_path)
    store = RunStore.create(tmp_path / "runs")
    result = _execute(repo, "task", config, store)
    manifest = json.loads((store.path / "run-manifest.json").read_text(encoding="utf-8"))
    assert result.stop_reason == "submitted"
    assert not Path(manifest["workspace"]).exists()


def test_workspace_base_commit_is_verified(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "x").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "x"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
    request = AgentRunRequest("task", repo, "0" * 40)
    with pytest.raises(EvalError, match="base commit"):
        request.verify_repository()
