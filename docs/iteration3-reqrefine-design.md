# Iteration 3: ReqRefine Design

## Research question

Does a structured, pre-execution requirement refinement layer improve fuzzy coding-task resolution while preserving full-prompt performance?

ReqRefine — Ontology-Guided Requirement Refinement adds one internal clarification phase before the existing Coding Agent can mutate or execute code:

`task → ontology parse → ambiguity routing → repository evidence → reflection gate → RequirementBaseline → existing AgentLoop`

## Minimal integration

`AgentLoop` remains the execution owner. A focused `reqagent.requirements` module owns the static ontology, skill catalog, baseline validation, gate state, prompt rendering, and sanitized evidence. `ToolRegistry` consults that state before dispatch: `list_files`, `read_file`, `search_text`, and `record_requirement_baseline` are available before gate approval; `apply_patch`, `run_command`, and `submit` return a structured `requirement_gate` rejection until approval.

After approval, `AgentLoop` inserts the normalized baseline as a system context message and continues the existing loop. Checkpoints serialize the full refinement state and bind it into resume identity. Completion writes `requirement-baseline.json` and `refinement-trace.json`, includes them in checksums, and never includes absolute paths, credentials, evaluator data, or external evidence.

## Static ontology

The ontology is a versioned constant with four aspects and eleven slots:

- Change Intent: `goal`, `current_behavior_or_symptom`, `expected_behavior`
- Code Scope: `target_component`, `relevant_symbol_or_api`, `affected_consumers`
- Constraints: `compatibility`, `boundary_and_error_semantics`, `excluded_scope`
- Validation: `acceptance_criteria`, `relevant_tests_or_checks`

Each slot contains `value`, `status` (`explicit`, `inferred`, `defaulted`, or `unresolved`), repository-relative `evidence`, and confidence in `[0, 1]`. Assumptions separately record value, provenance, and confidence.

## Skill routing

The static catalog contains `omission_recovery`, `reference_resolution`, and `specificity_expansion`. Every entry has `id`, `intent`, `use_when`, `avoid_when`, ordered `steps`, `stop_condition`, and `enabled`. The model may route at most two enabled skills. Complete prompts use a fast path and record explicit slots without unnecessary searches. Fuzzy prompts use only relevant repository reads/searches; uncertainty remains visibly inferred, defaulted, or unresolved.

## Reflection gate

`record_requirement_baseline` validates:

1. non-empty goal;
2. target/scope backed by repository-relative evidence, or explicitly conservative inference with provenance;
3. expected behavior or acceptance criteria is non-empty;
4. every assumption has provenance;
5. statuses are honest and confidence values valid;
6. no secret-shaped values, absolute paths, hidden evaluator/Oracle language, or repository-external evidence;
7. no more than two valid enabled skills.

A rejected baseline does not unlock mutation. The tool returns stable validation errors so the model can repair its submission. No user interaction is introduced.

## Evaluation namespace

Iteration 3 minimally parameterizes existing evaluation paths and evidence roots through an explicit iteration value. Iteration 2 remains the default-compatible implementation and is never rewritten. Iteration 3 uses `artifacts/runs/iteration3`, `audit/iteration3`, `baseline-v2`, E3/E4 labels, the unchanged 24-cell plan, `deterministic_wave_v1`, and `parallel_cells=2`.

Development accepts exactly one smoke cell and records `scope=single_cell_smoke`, the actual identity/status, behavior bindings, model, scheduler, and concurrency. Freeze binds source, prompts, config, schemas, ontology, skills, reflection policy, test receipt, development run, image inventory, and behavior hash.

## Tests and exclusions

Three scripted-model tests cover pre-gate denial, post-baseline continuation, and checkpoint/resume preservation. Only directly affected tests, `py_compile`, and `git diff --check` run before the targeted receipt.

This iteration does not implement ontology induction, real interviews, multiple agents, harness evolution, long-term memory, embeddings, new dependencies/services/models, UI, per-case rules, evaluator changes, or infrastructure refactoring. Formal prompts, official tests, Oracle data, and hidden evaluator content are never modified or exposed.
