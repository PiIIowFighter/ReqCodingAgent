from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from evalsys.agent_runner import AgentRunRequest
from evalsys.baseline import FORMAL_SEED, build_formal_plan, verify_formal_plan
from evalsys.cli import build_parser
from evalsys.errors import EvalError
from evalsys.harness import HarnessInvocation, build_harness_command
from evalsys.iteration2 import (
    build_agent_container_command,
    development_cells,
    prepare_task_repository,
    classify_cell_for_resume,
    freeze_baseline,
    official_image_name,
    resolve_image_identity,
    select_public_case,
    summarize_formal_results,
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
    formal = parser.parse_args(["run-formal", "--name", "baseline-v1", "--confirm"])
    assert not hasattr(formal, "config") and not hasattr(formal, "variant")
    report = parser.parse_args(["report", "--name", "baseline-v1"])
    assert report.name == "baseline-v1"


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


def test_agent_evaluator_harness_applies_prediction_patch(tmp_path: Path):
    invocation = HarnessInvocation(tmp_path, tmp_path / "adapter.py", tmp_path / "dataset.json", tmp_path / "prediction.jsonl", tmp_path / "report", "run", "instance", "agent", 1800)
    command = build_harness_command(invocation, platform_name="linux", python_executable="python")
    assert "--skip-patch" not in command
    assert command[command.index("--predictions") + 1] == str(invocation.predictions_path)


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


def test_prepare_task_repository_exports_only_testbed_at_base_commit(tmp_path: Path, monkeypatch):
    captured = []
    def runner(argv, **kwargs):
        captured.append(argv)
        destination = Path(argv[argv.index("--mount") + 1].split("src=", 1)[1].split(",", 1)[0])
        subprocess.run(["git", "-C", str(destination), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(destination), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(destination), "config", "user.name", "Test"], check=True)
        (destination / "tracked.txt").write_text("content\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(destination), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(destination), "commit", "-qm", "initial"], check=True)
        return subprocess.CompletedProcess(argv, 0, "", "")
    destination = tmp_path / "task"
    monkeypatch.setattr("evalsys.iteration2._git", lambda root, *args: "b" * 40 if args[0] == "rev-parse" else "")
    result = prepare_task_repository(
        docker_prefix=["docker"], image="sha256:" + "a" * 64,
        base_commit="b" * 40, destination=destination, run_id="run-1", runner=runner,
    )
    assert result == destination.resolve()
    assert (destination / "tracked.txt").read_text(encoding="utf-8") == "content\n"
    command = captured[0]
    assert command[command.index("--pull") + 1] == "never"
    assert command[command.index("--network") + 1] == "none"
    assert "checkout --detach " + "b" * 40 in command[-1]


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


def test_formal_report_counts_pairs_categories_and_usage():
    rows = []
    categories = ["omission"] * 4 + ["specificity_reduction"] * 4 + ["referential_ambiguity"] * 4
    for index, category in enumerate(categories):
        for variant in ("full", "fuzzy"):
            rows.append({
                "instance_id": f"case-{index}", "variant": variant, "ambiguity_type": category,
                "status": "resolved" if variant == "full" or index == 0 else "unresolved",
                "run_id": f"run-{index}-{variant}", "steps": 2, "tool_calls": 3,
                "usage": {"input_tokens": 10, "output_tokens": 4}, "wall_time_seconds": 5,
                "patch": {"files": 1, "additions": 1, "deletions": 0, "bytes": 20},
                "stop_reason": "submitted", "agent_tests": [], "evaluator_recorded": True,
            })
    report = summarize_formal_results(rows)
    assert report["E1_resolved"] == {"count": 12, "total": 12}
    assert report["E2_resolved"] == {"count": 1, "total": 12}
    assert report["absolute_drop"] == 11
    assert report["paired_outcomes"] == {"both": 1, "full_only": 11, "fuzzy_only": 0, "neither": 0}
    assert report["categories"]["omission"]["E2"] == {"count": 1, "total": 4}
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


def test_freeze_requires_authorization_development_and_image_identities(tmp_path: Path):
    records = [record for record in _records() if record["split"] == "test"]
    config = json.loads((ROOT / "configs/agent/live-local-proxy.json").read_text(encoding="utf-8"))
    config["budgets"].pop("max_invalid_outputs", None)
    config["budgets"].update({"max_consecutive_invalid_outputs": 3, "max_total_invalid_outputs": 6, "max_steps": 30, "max_tool_calls": 60, "wall_clock_seconds": 1800, "command_timeout_seconds": 300, "max_retries": 3})
    with pytest.raises(EvalError, match="authorization"):
        freeze_baseline(tmp_path, "baseline-v1", config, records, development=None, image_identities={}, authorized=False, git_commit="a" * 40)
    with pytest.raises(EvalError, match="development"):
        freeze_baseline(tmp_path, "baseline-v1", config, records, development=None, image_identities={}, authorized=True, git_commit="a" * 40)
    development = {"source_run_ids": [f"run-{index}" for index in range(6)]}
    missing = {f"image-{index}": {"available": index != 14, "image_id": "sha256:" + "a" * 64 if index != 14 else None} for index in range(15)}
    with pytest.raises(EvalError, match="must be resolved"):
        freeze_baseline(tmp_path, "baseline-v1", config, records, development=development, image_identities=missing, authorized=True, git_commit="a" * 40)


def test_freeze_writes_prompt_schema_and_lock_hashes(tmp_path: Path, monkeypatch):
    (tmp_path / "prompts/baseline").mkdir(parents=True)
    (tmp_path / "prompts/baseline/system.txt").write_text("system\n", encoding="utf-8")
    (tmp_path / "prompts/baseline/protocol.txt").write_text("protocol\n", encoding="utf-8")
    (tmp_path / "benchmark/manifests").mkdir(parents=True)
    (tmp_path / "benchmark/manifests/paired-cases.jsonl").write_text("manifest\n", encoding="utf-8")
    (tmp_path / "benchmark/source-lock.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("lock\n", encoding="utf-8")
    monkeypatch.setattr("evalsys.iteration2._tool_schemas", lambda root, config: [{"name": "tool"}])
    config = json.loads((ROOT / "configs/agent/live-local-proxy.json").read_text(encoding="utf-8"))
    development = {"source_run_ids": [f"run-{index}" for index in range(6)]}
    images = {f"image-{index}": {"image_id": "sha256:" + "a" * 64} for index in range(15)}
    root = freeze_baseline(tmp_path, "baseline-v1", config, [record for record in _records() if record["split"] == "test"], development=development, image_identities=images, authorized=True, git_commit="a" * 40)
    manifest = json.loads((root / "baseline.json").read_text(encoding="utf-8"))
    assert manifest["provider_hard_context_limit"] == "unavailable"
    assert manifest["authorization"]["kind"] == "conditional_pre_authorization"
    assert {"system.txt", "protocol.txt", "tool-schemas.json"}.issubset(path.name for path in root.iterdir())
    verify_frozen_baseline(root)


def test_verify_frozen_baseline_detects_hash_mismatch(tmp_path: Path):
    root = tmp_path / "baseline"
    root.mkdir()
    (root / "baseline.json").write_text("{}\n", encoding="utf-8")
    (root / "plan.json").write_text("[]\n", encoding="utf-8")
    baseline_hash = __import__("hashlib").sha256((root / "baseline.json").read_bytes()).hexdigest()
    plan_hash = __import__("hashlib").sha256((root / "plan.json").read_bytes()).hexdigest()
    (root / "checksums.sha256").write_text(f"{baseline_hash}  baseline.json\n{plan_hash}  plan.json\n", encoding="utf-8")
    verify_frozen_baseline(root)
    (root / "plan.json").write_text("[1]\n", encoding="utf-8")
    with pytest.raises(EvalError, match="checksum"):
        verify_frozen_baseline(root)


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
