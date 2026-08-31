# Iteration 3 ReqRefine Implementation Plan

> **For agentic workers:** Execute inline in this root session. Subagents, Task tooling, and code-review tooling are forbidden for this iteration. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add ontology-guided pre-execution requirement refinement, validate it with one development fuzzy cell, freeze `baseline-v2`, run fresh E3/E4 formal cells, and report an honest v1/v2 comparison.

**Architecture:** Add one focused refinement state object shared by `AgentLoop` and `ToolRegistry`; preserve the existing loop and tools after a valid baseline. Parameterize the existing iteration-2 evaluation machinery only where paths, development scope, experiment labels, frozen snapshots, and comparison reporting differ.

**Tech Stack:** Python 3.11, existing `jsonschema`, Git CLI, pytest, existing local proxy/model and pinned SWE-bench images.

## Global Constraints

- Never modify or rerun baseline-v1 or iteration2 evidence.
- Never access benchmark/private, Oracle/gold data, or use cell-specific behavior.
- Do not add dependencies, models, services, agents, Docker image operations, or broad infrastructure refactors.
- Run only the requested targeted tests, directly affected reqagent tests, py_compile, and git diff --check.
- Use only `D-S1-fuzzy` for development; accept the first legal non-infra result.
- Freeze before formal evaluation; never change behavior after freeze.
- Run formal work in two-pair batches with maximum concurrency two; retry only evaluator infrastructure failures.

---

### Task 1: Document the frozen design

**Files:**
- Create: `docs/iteration3-reqrefine-design.md`
- Create: `docs/superpowers/plans/2026-08-31-iteration3-reqrefine.md`

- [ ] Record integration, ontology, skills, gate, evidence, namespace, tests, and exclusions without paper titles or private content.
- [ ] Run `git diff --check` and commit `docs(iter3): define ontology-guided requirement refinement`.
- [ ] Push normally and verify local HEAD equals `origin/main`.

### Task 2: Implement refinement with tests first

**Files:**
- Create: `src/reqagent/requirements.py`
- Create: `src/reqagent/tools/requirements.py`
- Modify: `src/reqagent/tools/base.py`, `src/reqagent/tools/__init__.py`
- Modify: `src/reqagent/loop.py`, `src/reqagent/checkpoint.py`, `src/reqagent/cli.py`, `src/reqagent/context.py`
- Modify: `prompts/baseline/system.txt`, `prompts/baseline/protocol.txt`
- Test: `tests/test_reqagent_refinement.py`

**Interfaces:**
- Produce `RequirementRefinementState(task)`, `record(arguments) -> ToolEnvelope`, `to_checkpoint()`, `restore(value)`, `baseline`, `trace`, and `context_message()`.
- Produce `record_requirement_baseline` with strict schema for ambiguity types, selected skills, eleven slots, assumptions, and before/after summaries.
- Extend `ToolRegistry` with an optional pre-execution guard and expose unchanged post-gate tool behavior.

- [ ] Write scripted tests proving `apply_patch`, `run_command`, and `submit` are rejected before baseline approval.
- [ ] Run the new tests and observe gate-related failure.
- [ ] Implement static ontology/catalog, schema, sensitive-data/provenance validation, route limit, gate state, and sanitized serialization.
- [ ] Integrate the tool and dispatch guard; append exactly one normalized context message on approval.
- [ ] Extend checkpoints, resume identity, restoration, static evidence, completion checksums, and context compaction preservation.
- [ ] Write and pass scripted tests for valid continuation and interrupted resume preserving baseline/catalog/trace.
- [ ] Run only new and directly affected reqagent tests, py_compile, and `git diff --check`.
- [ ] Commit source/prompt changes as `feat(agent): add pre-execution requirement refinement` and tests/receipt as `test(agent): verify requirement refinement gate`; push each normally.

### Task 3: Add iteration3 evaluation namespace

**Files:**
- Create: `src/evalsys/iteration3.py`
- Modify: `src/evalsys/cli.py`, `src/evalsys/baseline.py`
- Test: `tests/test_iteration3_pipeline.py`
- Create: `audit/iteration3/test-receipt.json`

**Interfaces:**
- Wrap/reuse stable iteration2 primitives while passing `iteration=3`, E3/E4 labels, single-cell development plan, and iteration3 roots.
- Extend frozen snapshots with ontology, skill catalog, and reflection policy hashes.
- Produce `summarize_iteration3_results` and `compare_v1_v2` from aggregate iteration2 baseline inputs and fresh iteration3 rows.

- [ ] Add tests for iteration3 paths, single smoke development identity, freeze bindings, unchanged 24-cell plan, and comparison transitions.
- [ ] Implement minimal namespace/configuration without editing iteration2 artifacts.
- [ ] Run the targeted pipeline tests and generate `profile=iteration3_targeted` receipt with exact commands/counts and behavior bindings.

### Task 4: Run one development cell and freeze

**Files:**
- Create: `artifacts/runs/iteration3/...`
- Create: `audit/iteration3/development/v001.json` and sanitized run evidence.
- Create: `configs/frozen/baseline-v2/*`

- [ ] Recheck clean Git, local/remote equality, model/provider, image/evaluator, and receipt gates.
- [ ] Run only `D-S1-fuzzy` with one worker and existing fixed budget/image.
- [ ] If evaluator infrastructure fails, create a superseding retry; otherwise accept the first legal terminal result regardless of resolution.
- [ ] Record truthful `scope=single_cell_smoke` development evidence and commit/push `test(eval): record iteration-three development v001`.
- [ ] Freeze `baseline-v2` against the unchanged 24-cell plan and all ReqRefine behavior snapshots.
- [ ] Verify hashes and commit/push `chore(eval): freeze iteration-three baseline`.

### Task 5: Run E3/E4 and report

**Files:**
- Create: `artifacts/runs/iteration3/formal/baseline-v2/...`
- Create: `audit/iteration3/runs/...`, `audit/iteration3/reports/baseline-v2.json`, `audit/iteration3/reports/comparison-v1-v2.json`

- [ ] Run two complete pairs per batch, preserving pair order and maximum concurrency two.
- [ ] After each batch verify evidence/checksums/DAG, commit and push; resume automatically until 24/24.
- [ ] Supersede only evaluator-infra failures; retain all old attempts and never rerun legal outcomes.
- [ ] Generate baseline-v2 and v1/v2 comparison reports with counts/rates, 12 transitions, category changes, full regression signal, classifications, stop reasons, usage, time, patch, infrastructure, hashes, and actual model.
- [ ] Run final checksum/metadata/DAG/path/secret/residual-container and Git checks.
- [ ] Commit/push `test(eval): complete iteration-three formal evaluation`, then report results and stop.
