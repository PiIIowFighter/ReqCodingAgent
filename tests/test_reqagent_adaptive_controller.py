from __future__ import annotations

import inspect
import json
import subprocess
from pathlib import Path

import pytest

from reqagent.adaptive import (
    AdaptiveRefinementState,
    evidence_policy,
    rank_candidates,
    route_task,
)
from reqagent.config import AgentConfig
from reqagent.context import ContextLedger
from reqagent.loop import AgentLoop
from reqagent.model import ModelRequest, ModelResponse
from reqagent.tools import build_registry
from reqagent.workspace import GitWorkspace


ROOT = Path(__file__).resolve().parents[1]


def repository(tmp_path: Path) -> GitWorkspace:
    source = tmp_path / "repo"
    source.mkdir(parents=True)
    subprocess.run(["git", "-C", str(source), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
    (source / "parser.py").write_text("def parse(value):\n    return value\n", encoding="utf-8")
    (source / "test_parser.py").write_text("from parser import parse\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "initial"], check=True)
    return GitWorkspace.create(source)


def config() -> AgentConfig:
    return AgentConfig.load(ROOT / "configs/agent/offline-scripted.json")


def brief(evidence_ids: list[str]) -> dict:
    return {
        "ambiguity_reason": "The requested handling is not observable.",
        "chosen_interpretation": "Normalize empty input without changing valid input.",
        "targets": ["parser.parse"],
        "expected_behavior": "Empty input returns an empty value and valid input is unchanged.",
        "regression_invariants": ["Valid inputs retain their values."],
        "validation_plan": ["Run the focused parser tests before and after the patch."],
        "unresolved_uncertainty": [],
        "evidence_ids": evidence_ids,
        "candidates": [
            {"interpretation": "Normalize empty input", "task_fit": 4, "repository_support": 4, "compatibility": 4, "testability": 4},
            {"interpretation": "Rewrite all parsing", "task_fit": 1, "repository_support": 1, "compatibility": 0, "testability": 1},
        ],
    }


def test_router_uses_only_task_and_full_fast_path_has_baseline_tools(tmp_path: Path):
    task = "In parser.py, update parse(value) so empty strings return None while non-empty values remain unchanged; run test_parser.py."
    assert tuple(inspect.signature(route_task).parameters) == ("task",)
    decision = route_task(task)
    assert decision.mode == "fast"
    registry = build_registry(repository(tmp_path), config().raw, requirement_refinement="auto", task=task)
    assert [definition.name for definition in registry.definitions] == ["list_files", "read_file", "search_text", "apply_patch", "run_command", "submit"]
    assert registry.execute("submit", {"summary": "done", "tests": [], "limitations": ""}).ok


def test_fast_path_first_request_matches_frozen_baseline_v1_golden(tmp_path: Path):
    task = "In parser.py, update parse(value) so empty strings return None while non-empty values remain unchanged; run test_parser.py."
    workspace = repository(tmp_path)
    cfg = config()
    registry = build_registry(workspace, cfg.raw, requirement_refinement="auto", task=task)
    captured = []

    class Capture:
        position = 0
        identity = {"provider": "scripted"}
        def complete(self, request: ModelRequest) -> ModelResponse:
            captured.append(request)
            raise RuntimeError("captured")

    system = (ROOT / "prompts/baseline/system.txt").read_text(encoding="utf-8")
    protocol = (ROOT / "prompts/baseline/protocol.txt").read_text(encoding="utf-8")
    ledger = ContextLedger(system + "\n" + protocol, task, context_window=10000, trigger_ratio=.8, keep_recent_rounds=2)
    from reqagent.trace import RunStore
    store = RunStore.create(tmp_path / "runs")
    (store.path / "events.jsonl").write_text("", encoding="utf-8")
    AgentLoop(Capture(), registry, workspace, cfg, ledger, store).run()
    request = captured[0]
    golden_system = (ROOT / "configs/frozen/baseline-v1/system.txt").read_text(encoding="utf-8")
    golden_protocol = (ROOT / "configs/frozen/baseline-v1/protocol.txt").read_text(encoding="utf-8")
    assert request.messages[0].text == golden_system + "\n" + golden_protocol
    assert request.messages[1].text == task
    assert len(request.messages) == 2
    assert [tool.name for tool in request.tools] == ["list_files", "read_file", "search_text", "apply_patch", "run_command", "submit"]
    frozen_tools = json.loads((ROOT / "configs/frozen/baseline-v1/tool-schemas.json").read_text(encoding="utf-8"))
    assert [tool.__dict__ for tool in request.tools] == frozen_tools
    assert all("requirement" not in tool.name for tool in request.tools)
    assert cfg.budgets["max_steps"] == 30 and cfg.budgets["max_tool_calls"] == 60


def test_ambiguous_task_registers_only_compact_refinement(tmp_path: Path):
    state = AdaptiveRefinementState("Improve the related handling correctly.")
    registry = build_registry(repository(tmp_path), config().raw, requirement_refinement="auto", task=state.task)
    definitions = {definition.name: definition for definition in registry.definitions}
    assert registry.adaptive.route.mode == "refine"
    assert "record_requirement_brief" in definitions
    assert "record_requirement_baseline" not in definitions
    assert len(json.dumps(definitions["record_requirement_brief"].input_schema, separators=(",", ":")).encode()) < 3072


def test_evidence_ids_must_come_from_real_read_or_search_and_schema_disappears(tmp_path: Path):
    registry = build_registry(repository(tmp_path), config().raw, requirement_refinement="auto", task="Improve the related handling correctly.")
    rejected = registry.execute("record_requirement_brief", brief(["E999"]))
    assert not rejected.ok and rejected.error["kind"] == "requirement_gate"
    read = registry.execute("read_file", {"path": "parser.py"})
    evidence_id = read.meta["evidence_id"]
    accepted = registry.execute("record_requirement_brief", brief([evidence_id]))
    assert accepted.ok
    assert "record_requirement_brief" not in {definition.name for definition in registry.definitions}
    assert len(registry.adaptive.brief_message().encode()) <= 3072


def test_skill_policies_and_candidate_rerank_are_executable():
    policies = {name: evidence_policy(name) for name in ("omission_recovery", "reference_resolution", "specificity_expansion")}
    assert len({tuple(policy["evidence_order"]) for policy in policies.values()}) == 3
    ranked = rank_candidates([
        {"interpretation": "broad", "task_fit": 2, "repository_support": 1, "compatibility": 0, "testability": 1},
        {"interpretation": "focused", "task_fit": 4, "repository_support": 4, "compatibility": 4, "testability": 4},
    ])
    assert ranked[0]["interpretation"] == "focused"
    assert ranked[0]["score"] > ranked[1]["score"]


def test_reflection_can_reopen_once_and_checkpoint_restores_state(tmp_path: Path):
    registry = build_registry(repository(tmp_path), config().raw, requirement_refinement="auto", task="Improve the related handling correctly.")
    evidence_id = registry.execute("read_file", {"path": "parser.py"}).meta["evidence_id"]
    assert registry.execute("record_requirement_brief", brief([evidence_id])).ok
    revision = registry.execute("reflect_on_patch", {"decision": "revise", "reason": "Focused test contradicts the chosen interpretation."})
    assert revision.ok and registry.adaptive.revision_count == 1
    assert "record_requirement_brief" in {definition.name for definition in registry.definitions}
    checkpoint = registry.adaptive.to_checkpoint()
    restored = AdaptiveRefinementState(registry.adaptive.task)
    restored.restore(checkpoint)
    assert restored.phase == registry.adaptive.phase and restored.revision_count == 1
    accepted, _ = restored.reflect("revise", "A second contradiction.")
    assert accepted and restored.phase == "accepted"
    assert restored.reflection["decision"] == "accept"


def test_checkpoint_restores_approved_schema_and_phase_usage(tmp_path: Path):
    registry = build_registry(repository(tmp_path), config().raw, requirement_refinement="auto", task="Improve the related handling correctly.")
    evidence_id = registry.execute("read_file", {"path": "parser.py"}).meta["evidence_id"]
    assert registry.execute("record_requirement_brief", brief([evidence_id])).ok
    registry.adaptive.add_usage("refinement", {"input_tokens": 7, "output_tokens": 2})
    registry.adaptive.add_usage("main", {"input_tokens": 11, "output_tokens": 3})
    checkpoint = registry.adaptive.to_checkpoint()
    restored_registry = build_registry(repository(tmp_path / "restored"), config().raw, requirement_refinement="auto", task=registry.adaptive.task)
    restored_registry.adaptive.restore(checkpoint)
    assert restored_registry.adaptive.phase == "coding"
    assert restored_registry.adaptive.schema_removed is True
    assert "record_requirement_brief" not in {definition.name for definition in restored_registry.definitions}
    assert restored_registry.adaptive.evidence == registry.adaptive.evidence
    trace = restored_registry.adaptive.trace()
    assert trace["usage_by_phase"]["router"] == {}
    assert trace["usage"] == {"input_tokens": 18, "output_tokens": 5}
    assert trace["steps_by_phase"]["main"] == 0


def test_reflection_only_appears_after_refined_patch_contradiction(tmp_path: Path):
    registry = build_registry(repository(tmp_path), config().raw, requirement_refinement="auto", task="Improve the related handling correctly.")
    evidence_id = registry.execute("read_file", {"path": "parser.py"}).meta["evidence_id"]
    assert registry.execute("record_requirement_brief", brief([evidence_id])).ok
    assert "reflect_on_patch" not in {definition.name for definition in registry.definitions}
    registry.adaptive.observe_tool("apply_patch", True, None, True)
    registry.adaptive.observe_tool("run_command", False, {"kind": "nonzero_exit"}, False)
    assert "reflect_on_patch" in {definition.name for definition in registry.definitions}
    assert not registry.execute("submit", {"summary": "best patch", "tests": [], "limitations": "test failed"}).ok
    registry.adaptive.fail_open_reflection("reflection model timeout")
    assert registry.execute("submit", {"summary": "best patch", "tests": [], "limitations": "test failed"}).ok
