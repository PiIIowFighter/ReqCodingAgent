from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from evalsys.errors import EvalError
from evalsys.isolation import ContainerRunner, construct_agent_workspace, prove_isolation


class FakeContainerRunner(ContainerRunner):
    def __init__(self, result: subprocess.CompletedProcess[str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.result = result

    def run(self, argv: list[str], *, timeout_s: int = 30) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if "wslpath" in argv:
            return subprocess.CompletedProcess(argv, 0, "/mnt/c/agent-workspace", "")
        return self.result or subprocess.CompletedProcess(argv, 0, '{"positive":true,"negative":true}', "")


def _public_case(prompt: str = "PUBLIC PROMPT") -> dict:
    return {
        "schema_version": "1.0", "case_id": "case-1-fuzzy", "split": "test", "pair_id": "case-1",
        "prompt_variant": "fuzzy", "instance_id": "owner__repo-1", "repo": "owner/repo",
        "base_commit": "a" * 40, "ambiguity_type": "omission", "severity": "medium",
        "source_dataset": "SWE-bench Verified", "source_revision": "b" * 40,
        "original_prompt_sha256": "c" * 64,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(), "prompt": prompt,
        "approval_status": "frozen", "transformation_description": "frozen transformation",
        "hidden_fact_id": "HF-1", "FAIL_TO_PASS": ["test_fails"], "PASS_TO_PASS": ["test_passes"],
    }


def _fixture(tmp_path: Path):
    project = tmp_path / "project"
    repo = tmp_path / "clean-task-repo"
    destination = tmp_path / "agent-workspace"
    repo.mkdir(parents=True)
    (repo / "task.py").write_text("print('allowed')\n", encoding="utf-8")
    forbidden = {
        "benchmark/private/oracle.txt": "EVALSYS_PRIVATE_CANARY_ORACLE_93b2",
        "benchmark/private/gold.patch": "EVALSYS_PRIVATE_CANARY_GOLD_a151",
        "benchmark/private/test.patch": "EVALSYS_PRIVATE_CANARY_TEST_499d",
        "benchmark/private/hints.txt": "EVALSYS_PRIVATE_CANARY_HINT_d021",
        "计划/secret.txt": "EVALSYS_PRIVATE_CANARY_PLAN_a992",
        "资料/secret.txt": "EVALSYS_PRIVATE_CANARY_MATERIAL_b173",
        "artifacts/evaluator.log": "EVALSYS_PRIVATE_CANARY_LOG_36d0",
        ".cache/evaluator.cache": "EVALSYS_PRIVATE_CANARY_CACHE_ff82",
    }
    for relative, canary in forbidden.items():
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(canary, encoding="utf-8")
    return project, repo, destination, _public_case(), forbidden


def test_constructor_copies_only_clean_repo_and_projected_prompt(tmp_path: Path):
    project, repo, destination, case, forbidden = _fixture(tmp_path)
    manifest = construct_agent_workspace(repo, case, destination, project_root=project)
    prompt = json.loads((destination / "prompt.json").read_text(encoding="utf-8"))
    assert prompt == {"case_id": "case-1-fuzzy", "prompt_variant": "fuzzy", "prompt": "PUBLIC PROMPT", "prompt_sha256": case["prompt_sha256"]}
    assert set(path.relative_to(destination).as_posix() for path in destination.rglob("*") if path.is_file()) == {
        "repo/task.py", "repo/.evalsys-allowed-repo", "prompt.json", "workspace-manifest.json"
    }
    workspace_text = "\n".join(path.read_text(encoding="utf-8") for path in destination.rglob("*") if path.is_file())
    assert all(canary not in workspace_text for canary in forbidden.values())
    assert manifest["source_project_root_mounted"] is False


@pytest.mark.parametrize("relationship", ["repo_is_project_parent", "destination_is_project_parent", "repo_contains_destination", "destination_contains_repo"])
def test_constructor_rejects_bidirectional_path_overlap(tmp_path: Path, relationship: str):
    project, repo, destination, case, _ = _fixture(tmp_path)
    if relationship == "repo_is_project_parent":
        project = repo / "evaluator"
        project.mkdir()
    elif relationship == "destination_is_project_parent":
        project = destination / "evaluator"
        project.mkdir(parents=True)
    elif relationship == "repo_contains_destination":
        destination = repo / "workspace"
    else:
        destination = tmp_path / "outer"
        repo = destination / "task"
        repo.mkdir(parents=True)
        (repo / "task.py").write_text("ok", encoding="utf-8")
    with pytest.raises(EvalError, match="overlap|contain"):
        construct_agent_workspace(repo, case, destination, project_root=project)


def test_constructor_rejects_portable_symlink_escape(tmp_path: Path):
    project, repo, destination, case, _ = _fixture(tmp_path)
    outside = tmp_path / "outside-secret"
    outside.write_text("EVALSYS_PRIVATE_CANARY_ESCAPE", encoding="utf-8")
    try:
        (repo / "escape").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(EvalError, match="link|reparse|escape"):
        construct_agent_workspace(repo, case, destination, project_root=project)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction behavior")
def test_constructor_rejects_real_windows_junction(tmp_path: Path):
    project, repo, destination, case, _ = _fixture(tmp_path)
    outside = tmp_path / "junction-target"
    outside.mkdir()
    junction = repo / "junction"
    result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(junction), str(outside)], capture_output=True)
    if result.returncode != 0:
        pytest.skip("junction creation unavailable")
    assert not junction.is_symlink()
    with pytest.raises(EvalError, match="reparse"):
        construct_agent_workspace(repo, case, destination, project_root=project)


@pytest.mark.parametrize("mutation", ["bare", "fake_hash", "not_frozen", "private_extra"])
def test_constructor_strictly_validates_public_case(tmp_path: Path, mutation: str):
    project, repo, destination, case, _ = _fixture(tmp_path)
    if mutation == "bare":
        case = {key: case[key] for key in ("case_id", "prompt_variant", "prompt", "prompt_sha256")}
    elif mutation == "fake_hash":
        case["prompt_sha256"] = "d" * 64
    elif mutation == "not_frozen":
        case["approval_status"] = "draft"
    else:
        case["oracle_answer"] = "EVALSYS_PRIVATE_CANARY_IN_RECORD"
    with pytest.raises(EvalError, match="schema|hash"):
        construct_agent_workspace(repo, case, destination, project_root=project)


def test_constructor_rejects_private_canary_in_task_repo_before_copy(tmp_path: Path):
    project, repo, destination, case, forbidden = _fixture(tmp_path)
    (repo / "innocent.txt").write_text(next(iter(forbidden.values())), encoding="utf-8")
    with pytest.raises(EvalError, match="canary"):
        construct_agent_workspace(repo, case, destination, project_root=project)
    assert not destination.exists()


def test_windows_container_probe_uses_wsl_path_conversion(tmp_path: Path):
    project, repo, destination, case, _ = _fixture(tmp_path)
    runner = FakeContainerRunner()
    proof = prove_isolation(project, repo, case, destination, runner=runner, platform_name="win32")
    assert proof["status"] == "passed"
    assert runner.calls[0][:3] == ["wsl.exe", "--", "wslpath"]
    assert "\\" not in runner.calls[0][-1]
    assert runner.calls[1][:3] == ["wsl.exe", "--", "docker"]


def test_proof_runs_host_and_container_positive_and_negative_probes(tmp_path: Path):
    project, repo, destination, case, _ = _fixture(tmp_path)
    runner = FakeContainerRunner()
    proof = prove_isolation(project, repo, case, destination, runner=runner)
    assert proof["host_probe"] == proof["container_probe"] == {"positive": True, "negative": True}
    assert proof["forbidden_mounts"] == proof["forbidden_allowlist_entries"] == []
    serialized = json.dumps(proof)
    assert str(project) not in serialized and str(destination) not in serialized
    docker_call = runner.calls[-1]
    assert sum(value == "--mount" for value in docker_call) == 1


def test_container_failure_reports_sanitized_bounded_diagnostics(tmp_path: Path):
    project, repo, destination, case, _ = _fixture(tmp_path)
    secret_path = str(project / "benchmark/private/oracle.txt")
    runner = FakeContainerRunner(subprocess.CompletedProcess([], 125, f"mount failed {secret_path} token=abc " + "x" * 10000, "stderr password=hunter2"))
    with pytest.raises(EvalError) as caught:
        prove_isolation(project, repo, case, destination, runner=runner, platform_name="linux")
    message = str(caught.value)
    assert "returncode=125" in message and "mount failed" in message and "stderr" in message
    assert secret_path not in message and "abc" not in message and "hunter2" not in message
    assert len(message) < 2500
