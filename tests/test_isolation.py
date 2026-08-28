from __future__ import annotations

import json
import subprocess
from pathlib import Path

from evalsys.isolation import ContainerRunner, construct_agent_workspace, prove_isolation


class FakeContainerRunner(ContainerRunner):
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], *, timeout_s: int = 30) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if "wslpath" in argv:
            return subprocess.CompletedProcess(argv, 0, "/mnt/c/agent-workspace", "")
        return subprocess.CompletedProcess(argv, 0, '{"positive":true,"negative":true}', "")


def _fixture(tmp_path: Path):
    project = tmp_path / "project"
    repo = tmp_path / "clean-task-repo"
    destination = tmp_path / "agent-workspace"
    repo.mkdir(parents=True)
    (repo / "task.py").write_text("print('allowed')\n", encoding="utf-8")
    forbidden = {
        "benchmark/private/oracle.txt": "CANARY_ORACLE_93b2",
        "benchmark/private/gold.patch": "CANARY_GOLD_a151",
        "benchmark/private/test.patch": "CANARY_TEST_499d",
        "benchmark/private/hints.txt": "CANARY_HINT_d021",
        "计划/secret.txt": "CANARY_PLAN_a992",
        "资料/secret.txt": "CANARY_MATERIAL_b173",
        "artifacts/evaluator.log": "CANARY_LOG_36d0",
        ".cache/evaluator.cache": "CANARY_CACHE_ff82",
    }
    for relative, canary in forbidden.items():
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(canary, encoding="utf-8")
    case = {"case_id": "case-1-fuzzy", "prompt_variant": "fuzzy", "prompt": "PUBLIC PROMPT", "prompt_sha256": "a" * 64}
    return project, repo, destination, case, forbidden


def test_constructor_copies_only_clean_repo_and_one_public_prompt(tmp_path: Path):
    project, repo, destination, case, forbidden = _fixture(tmp_path)
    manifest = construct_agent_workspace(repo, case, destination, project_root=project)
    assert (destination / "repo/task.py").is_file()
    assert json.loads((destination / "prompt.json").read_text(encoding="utf-8"))["prompt"] == "PUBLIC PROMPT"
    assert set(path.relative_to(destination).as_posix() for path in destination.rglob("*") if path.is_file()) == {
        "repo/task.py", "repo/.evalsys-allowed-repo", "prompt.json", "workspace-manifest.json"
    }
    workspace_text = "\n".join(path.read_text(encoding="utf-8") for path in destination.rglob("*") if path.is_file())
    assert all(canary not in workspace_text for canary in forbidden.values())
    assert manifest["source_project_root_mounted"] is False
    assert manifest["prompt_variants"] == ["fuzzy"]


def test_windows_container_probe_uses_wsl_path_conversion(tmp_path: Path):
    project, repo, destination, case, _ = _fixture(tmp_path)
    runner = FakeContainerRunner()
    proof = prove_isolation(project, repo, case, destination, runner=runner, platform_name="win32")
    assert proof["status"] == "passed"
    assert runner.calls[0][:3] == ["wsl.exe", "--", "wslpath"]
    assert "\\" not in runner.calls[0][-1]
    assert runner.calls[1][:3] == ["wsl.exe", "--", "docker"]
    assert "source=/mnt/c/agent-workspace" in runner.calls[1][runner.calls[1].index("--mount") + 1]


def test_proof_runs_host_and_container_positive_and_negative_probes(tmp_path: Path):
    project, repo, destination, case, forbidden = _fixture(tmp_path)
    runner = FakeContainerRunner()
    proof = prove_isolation(project, repo, case, destination, runner=runner)
    assert proof["status"] == "passed"
    assert proof["host_probe"] == {"positive": True, "negative": True}
    assert proof["container_probe"] == {"positive": True, "negative": True}
    assert proof["forbidden_mounts"] == []
    assert proof["forbidden_allowlist_entries"] == []
    serialized = json.dumps(proof)
    assert str(project) not in serialized
    assert str(destination) not in serialized
    docker_call = runner.calls[-1]
    mounts = [docker_call[index + 1] for index, value in enumerate(docker_call) if value == "--mount"]
    assert len(mounts) == 1
    assert "agent-workspace" in mounts[0]
    assert all("benchmark/private" not in mount for mount in mounts)
