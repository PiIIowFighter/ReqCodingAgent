# Adaptive Evidence-Grounded ReqRefine Implementation Plan

> **Execution policy:** Inline root-session execution only; no subagents. Test-first changes, responsibility-scoped commits, and frozen-baseline discipline apply.

**Goal:** Replace baseline-v2's mandatory fixed form with adaptive evidence-grounded refinement, improve task test execution and evaluator stability, freeze baseline-v3, and run one fresh 24-cell matrix.

**Architecture:** A task-only controller selects a baseline-equivalent fast path or a temporary compact refinement phase. Frozen-image command bootstrap and evaluator-only serialization improve feedback and stability. Baseline-scoped reporting prevents v2/v3 evidence mixing.

**Tech Stack:** Python 3.11, existing jsonschema/pytest, frozen Docker images, Git.

## Global constraints

- Preserve baseline-v1, baseline-v2, audit/iteration2, and existing audit/iteration3 evidence.
- No evaluation metadata, case/repository identity, Oracle, gold, or hidden tests enter Agent behavior.
- Keep main budget 30 steps/60 tools; no dependencies, networking, image pull/build/prune/reset, or repeated formal matrix.
- Use only v003 single-cell development before baseline-v3.

### Task 1: Document diagnosis and design
- Create the diagnosis, design, and this implementation plan.
- Verify diff and commit `docs(iter3): redesign adaptive requirement refinement`.

### Task 2: Bootstrap frozen task environments
- Test deterministic interpreter/pytest bootstrap, fallback, no install/network, and evidence identity.
- Modify `reqagent.tools.command`/runtime evidence only.
- Commit `fix(agent): activate frozen task test environments`.

### Task 3: Implement adaptive controller
- Write focused tests for task-only router, full fast path, ambiguous path, brief size/evidence IDs, schema removal, distinct policies, scoring/rerank/gate, one revise, resume, and hardcoding scan.
- Replace fixed baseline runtime with controller while retaining audit ontology.
- Update loop/registry/context/checkpoint/evidence and prompts.
- Commit `feat(agent): add adaptive evidence-grounded refinement` and `test(agent): verify adaptive refinement and validation`.

### Task 4: Isolate evaluator critical section
- Write a short concurrency regression showing Agent workers overlap while evaluator max active is one.
- Add stable absolute cwd and evaluator-only lock.
- Commit `fix(eval): isolate concurrent evaluator working directories`.

### Task 5: Support multiple iteration3 baselines
- Test baseline-scoped active/ancestor closure and output names/comparisons.
- Parameterize report provenance and isolate v2/v3 accounting.
- Commit `test(eval): support revised iteration-three baseline reports`.

### Task 6: Verify, isolate, and run v003
- Run only targeted tests, one final quick pytest, py_compile, diff check, validate-data, scans, and one new isolation proof.
- Regenerate targeted receipt with final bindings and push clean behavior.
- Run only D-S1-fuzzy v003. Require refinement, bootstrap, traces, non-infra, and resolved status; diagnose before any successor if it regresses.
- Commit/push development evidence.

### Task 7: Freeze and evaluate baseline-v3
- Freeze baseline-v3 from clean synced Git and v003.
- Commit `chore(eval): freeze revised iteration-three baseline`.
- Run one fresh 24-cell matrix in four-cell batches, retrying only infra attempts, with evidence commits after safe batches.
- Generate baseline-v3 and both v1/v3 and v2/v3 comparisons.
- Verify checksums, metadata, DAG, baseline isolation, secrets, paths, containers, Git.
- Commit `test(eval): complete revised iteration-three evaluation` and stop.
