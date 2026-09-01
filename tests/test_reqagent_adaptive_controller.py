from __future__ import annotations

import inspect
import json
import subprocess
from pathlib import Path

import pytest

from reqagent.adaptive import (
    AdaptiveRefinementState,
    brief_schema,
    evidence_policy,
    rank_candidates,
    route_task,
)
from reqagent.checkpoint import CheckpointStore, canonical_hash
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


def test_router_treats_vague_greenfield_chinese_tasks_as_refine():
    decision = route_task("生成一个股票搜索网站")
    assert decision.mode == "refine"
    # Should trigger refinement due to missing concrete targets, validation, etc.
    assert "goal" in decision.reasons or "target" in decision.reasons or "observable_behavior" in decision.reasons or "validation" in decision.reasons


def test_router_treats_vague_greenfield_english_tasks_as_refine():
    decision = route_task("Build a stock search website")
    assert decision.mode == "refine"
    # Should trigger refinement due to missing concrete targets, validation, etc.
    assert "goal" in decision.reasons or "target" in decision.reasons or "observable_behavior" in decision.reasons or "validation" in decision.reasons


def test_router_treats_detailed_greenfield_tasks_as_fast():
    # A detailed task with targets, behavior, validation should still go fast
    task = "Create index.html with a search form containing an input#symbol and button#search. On click, fetch /api/stock?symbol=VALUE and display result in #output. Include error handling for 404. Test with manual browser verification."
    decision = route_task(task)
    assert decision.mode == "fast"
    assert decision.reasons == ("detailed_behavior_contract",)


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


def test_router_selects_distinct_runtime_skill_instructions_from_task_only():
    reference = AdaptiveRefinementState("Fix it so this related handling works.")
    specificity = AdaptiveRefinementState("Improve parsing behavior correctly without changing valid values.")
    assert reference.route.selected_skills != specificity.route.selected_skills
    assert "reference_resolution" in reference.route.selected_skills
    assert "specificity_expansion" in specificity.route.selected_skills
    reference_instruction = reference.refinement_instruction()
    specificity_instruction = specificity.refinement_instruction()
    assert reference_instruction != specificity_instruction
    for state, instruction in ((reference, reference_instruction), (specificity, specificity_instruction)):
        for skill_id in state.route.selected_skills:
            policy = evidence_policy(skill_id)
            assert skill_id in instruction
            assert all(item in instruction for item in policy["evidence_order"])
            assert all(item in instruction for item in policy["required_outputs"])
        assert "ontology" not in instruction.lower()


def test_ambiguous_task_registers_only_compact_refinement(tmp_path: Path):
    state = AdaptiveRefinementState("Improve the related handling correctly.")
    registry = build_registry(repository(tmp_path), config().raw, requirement_refinement="auto", task=state.task)
    definitions = {definition.name: definition for definition in registry.definitions}
    assert registry.adaptive.route.mode == "refine"
    assert set(definitions) == {"list_files", "read_file", "search_text"}
    schemas = {definition.name: definition for definition in registry.schema_definitions}
    assert "record_requirement_baseline" not in schemas
    assert len(json.dumps(schemas["record_requirement_brief"].input_schema, separators=(",", ":")).encode()) < 3072


def test_evidence_ids_must_come_from_real_read_or_search_and_schema_disappears(tmp_path: Path):
    registry = build_registry(repository(tmp_path), config().raw, requirement_refinement="auto", task="Improve the related handling correctly.")
    assert [tool.name for tool in registry.definitions] == ["list_files", "read_file", "search_text"]
    for hidden_name, arguments in (
        ("apply_patch", {"patch": ""}),
        ("run_command", {"command": "true"}),
        ("submit", {"summary": "early", "tests": [], "limitations": ""}),
        ("reflect_on_patch", {"decision": "accept", "reason": "too early"}),
    ):
        result = registry.execute(hidden_name, arguments)
        assert not result.ok and result.error["kind"] == "inactive_tool"
    read = registry.execute("read_file", {"path": "parser.py"})
    evidence_id = read.meta["evidence_id"]
    registry.adaptive.transition_to_synthesis()
    rejected = registry.execute("record_requirement_brief", brief(["E999"]))
    assert not rejected.ok and rejected.error["kind"] == "requirement_gate"
    accepted = registry.execute("record_requirement_brief", brief([evidence_id]))
    assert accepted.ok
    assert [tool.name for tool in registry.definitions] == ["list_files", "read_file", "search_text", "apply_patch", "run_command", "submit"]
    assert not registry.execute("record_requirement_brief", brief([evidence_id])).ok
    assert len(registry.adaptive.brief_message().encode()) <= 3072
    assert "refinement is complete" in registry.adaptive.brief_message().lower()


def test_brief_schema_explains_deterministic_top_ranked_interpretation():
    description = brief_schema()["properties"]["chosen_interpretation"]["description"]
    assert "top-ranked" in description.lower()
    assert "replaced" in description.lower()

    state = AdaptiveRefinementState("Improve the related handling correctly.")
    evidence_id = state.add_evidence("read_file", {"path": "parser.py"})
    value = brief([evidence_id])
    accepted, errors = state.record_brief(value)
    assert accepted and not errors
    assert value["chosen_interpretation"] != value["candidates"][0]["interpretation"]
    assert state.brief["chosen_interpretation"] == value["candidates"][0]["interpretation"]


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
    registry.adaptive.transition_to_synthesis()
    assert registry.execute("record_requirement_brief", brief([evidence_id])).ok
    registry.adaptive.observe_tool("apply_patch", True, None, True)
    registry.adaptive.observe_tool("run_command", False, {"kind": "nonzero_exit"}, False)
    assert [tool.name for tool in registry.definitions] == ["reflect_on_patch"]
    revision = registry.execute("reflect_on_patch", {"decision": "revise", "reason": "Focused test contradicts the chosen interpretation."})
    assert revision.ok and registry.adaptive.revision_count == 1
    assert {definition.name for definition in registry.definitions} == {"list_files", "read_file", "search_text"}
    checkpoint = registry.adaptive.to_checkpoint()
    restored = AdaptiveRefinementState(registry.adaptive.task)
    restored.restore(checkpoint)
    assert restored.phase == registry.adaptive.phase and restored.revision_count == 1
    accepted, _ = restored.reflect("revise", "A second contradiction.")
    assert accepted and restored.phase == "accepted"
    assert restored.reflection["decision"] == "accept"


def test_reflection_revision_resets_failed_synthesis_for_fresh_bounded_cycle():
    state = AdaptiveRefinementState("Improve the related handling correctly.")
    stale_evidence = state.add_evidence("read_file", {"path": "parser.py", "start_line": 1, "end_line": 2})
    accepted, errors = state.record_brief(brief([stale_evidence]))
    assert accepted and not errors
    state.investigation_response_count = 2
    state.investigation_tool_count = 5
    state.synthesis_response_count = 1
    state.synthesis_tool_count = 1
    state.fail_open_refinement("invalid synthesis brief")
    state.observe_tool("apply_patch", True, None, True)
    state.observe_tool("run_command", False, {"kind": "nonzero_exit"}, False)
    assert state.phase == "reflection"

    accepted, error = state.reflect("revise", "Validation contradicts the failed synthesis.")

    assert accepted and not error
    assert state.phase == "refining"
    assert state.refinement_stage == "investigating"
    assert state.evidence == {}
    assert state.brief is None
    assert state.ranked_candidates == []
    assert state.requires_reflection is False
    assert state.contradiction_reason == ""
    assert state.investigation_response_count == 0
    assert state.investigation_tool_count == 0
    assert state.synthesis_response_count == 0
    assert state.synthesis_tool_count == 0
    assert state.reflection_count == state.revision_count == 1

    replacement_evidence = state.add_evidence("read_file", {"path": "parser.py", "start_line": 1, "end_line": 3})
    state.transition_to_synthesis()
    accepted, errors = state.record_brief(brief([replacement_evidence]))
    assert accepted and not errors
    assert state.phase == "coding"
    assert state.refinement_stage == "complete"
    assert state.brief is not None


def test_checkpoint_restores_approved_schema_and_phase_usage(tmp_path: Path):
    registry = build_registry(repository(tmp_path), config().raw, requirement_refinement="auto", task="Improve the related handling correctly.")
    evidence_id = registry.execute("read_file", {"path": "parser.py"}).meta["evidence_id"]
    registry.adaptive.transition_to_synthesis()
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


def test_persisted_checkpoint_restores_dynamic_schema_and_fail_open(tmp_path: Path):
    task = "Improve the related handling correctly."
    registry = build_registry(repository(tmp_path), config().raw, requirement_refinement="auto", task=task)
    evidence_id = registry.execute("read_file", {"path": "parser.py"}).meta["evidence_id"]
    registry.adaptive.transition_to_synthesis()
    assert registry.execute("record_requirement_brief", brief([evidence_id])).ok
    payload = {"adaptive": registry.adaptive.to_checkpoint(), "schema_hash": canonical_hash([tool.__dict__ for tool in registry.definitions]), "tool_history": registry.history}
    run = tmp_path / "checkpoint-run"
    run.mkdir()
    store = CheckpointStore(run)
    store.save(1, payload)
    loaded = store.load()
    restored = build_registry(repository(tmp_path / "persisted"), config().raw, requirement_refinement="auto", task=task)
    restored.adaptive.restore(loaded["adaptive"])
    assert canonical_hash([tool.__dict__ for tool in restored.definitions]) == loaded["schema_hash"]
    assert "record_requirement_brief" not in {tool.name for tool in restored.definitions}
    assert restored.adaptive.evidence == registry.adaptive.evidence
    assert len(loaded["tool_history"]) == len(registry.history)

    fallback = build_registry(repository(tmp_path / "fallback"), config().raw, requirement_refinement="auto", task=task)
    original_route = fallback.adaptive.route
    fallback.adaptive.fail_open_refinement("model error")
    fallback_payload = {"adaptive": fallback.adaptive.to_checkpoint(), "schema_hash": canonical_hash([tool.__dict__ for tool in fallback.definitions]), "tool_history": []}
    fallback_run = tmp_path / "fallback-run"
    fallback_run.mkdir()
    fallback_store = CheckpointStore(fallback_run)
    fallback_store.save(1, fallback_payload)
    loaded_fallback = fallback_store.load()
    resumed_fallback = build_registry(repository(tmp_path / "fallback-resumed"), config().raw, requirement_refinement="auto", task=task)
    resumed_fallback.adaptive.restore(loaded_fallback["adaptive"])
    assert resumed_fallback.adaptive.route == original_route
    assert resumed_fallback.adaptive.phase == "coding"
    assert resumed_fallback.adaptive.schema_removed is True
    assert resumed_fallback.adaptive.fallback_reason == "model error"
    assert canonical_hash([tool.__dict__ for tool in resumed_fallback.definitions]) == loaded_fallback["schema_hash"]


def test_reflection_only_appears_after_refined_patch_contradiction(tmp_path: Path):
    registry = build_registry(repository(tmp_path), config().raw, requirement_refinement="auto", task="Improve the related handling correctly.")
    evidence_id = registry.execute("read_file", {"path": "parser.py"}).meta["evidence_id"]
    registry.adaptive.transition_to_synthesis()
    assert registry.execute("record_requirement_brief", brief([evidence_id])).ok
    assert "reflect_on_patch" not in {definition.name for definition in registry.definitions}
    registry.adaptive.observe_tool("apply_patch", True, None, True)
    registry.adaptive.observe_tool("run_command", False, {"kind": "nonzero_exit"}, False)
    assert "reflect_on_patch" in {definition.name for definition in registry.definitions}
    assert not registry.execute("submit", {"summary": "best patch", "tests": [], "limitations": "test failed"}).ok
    registry.adaptive.fail_open_reflection("reflection model timeout")
    assert registry.execute("submit", {"summary": "best patch", "tests": [], "limitations": "test failed"}).ok
