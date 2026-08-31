# Adaptive Evidence-Grounded ReqRefine Design

## Provenance and goal

Baseline-v2 remains the immutable first ReqRefine candidate. Baseline-v3 will be a post-hoc revised candidate informed by aggregate baseline-v2 error and cost analysis. The design targets full-prompt protection and improved fuzzy recovery without reading evaluation metadata or encoding repository/case rules.

## Adaptive routing

A deterministic `mode=auto` router receives only the original task string. It checks whether goal, target scope/symbol, observable expected behavior, and constraint/validation cues are sufficiently explicit. It never accepts case identity, repository identity, split, variant, ambiguity metadata, sequence, score, or evaluator fields.

Sufficient tasks and router failures take a fail-open fast path. The main loop retains the original task, baseline-style system/protocol, six ordinary tools, and the existing 30-step/60-tool budget. No requirement tool or rewritten contract is present.

Substantively ambiguous tasks enter a separately bounded refinement phase. Refinement overhead is recorded separately and does not consume main-loop steps or tools.

## Evidence controller and brief

Read/list/search results receive stable short evidence IDs. A compact temporary tool accepts a RequirementBrief containing ambiguity reason, chosen interpretation, target components/symbols, observable behavior, compatibility/regression invariants, validation plan, unresolved uncertainty, and evidence IDs. The serialized brief is capped at 3 KiB and every ID must exist in the controller ledger.

Rich ontology expansion is audit-only. After approval, the temporary tool disappears from `ModelRequest.tools`; one short system note identifies the brief as an evidence-backed working hypothesis subordinate to the original user task.

## Executable skill policies

- `omission_recovery`: tests and public API first, then callers and symmetric/inverse behavior; extracts missing input-output, boundaries, invariants, and an acceptance check.
- `reference_resolution`: generates at most three referents, gathers symbol/caller/test/documentation evidence, scores task fit/repository support/compatibility/testability, reranks, and performs one focused search if the top margin is insufficient.
- `specificity_expansion`: derives input-behavior-output plus normal, boundary, preserved behavior, and pre/post checks.

Candidate generation, scoring, reranking, gate pruning, and chosen/unresolved hypotheses are trace data and controller state, not prompt-only terminology.

## Post-patch reflection

Before submit, a compact reflection checks original-task satisfaction, current evidence support, target scope, relevant and neighboring tests, unrelated diff changes, and fallback static review. It returns accept or revise with a short reason. One revise is allowed; contradiction reopens the brief and returns to investigation. A second revise request is rejected to bound work.

## Frozen task environment

The command executor keeps the frozen official task image, no network, `--pull never`, existing mounts, and bounded resources. It executes a deterministic bootstrap script that discovers an existing testbed/conda interpreter, verifies Python and pytest availability, and then runs the command. No package installation occurs. The selected interpreter, pytest availability, fallback reason, and bootstrap identity are written as run evidence and surfaced once to the Agent.

## Evaluator isolation

Official evaluator adapter execution uses an absolute stable cwd and a process-local lock around only the adapter invocation. Model/Agent phases remain outside the lock and can reach concurrency two. The adapter no longer relies on a shared caller cwd. Existing infra supersession remains unchanged.

## Checkpoint, evidence, and reports

Checkpoints persist router decision/reason, phase, evidence ledger, candidates/scores, brief, schema-removal state, and reflection/revision count. Runs emit router, brief, rich ontology, evidence, environment bootstrap, and reflection traces.

Iteration3 reporting scopes runs by the requested manifest baseline: current active IDs plus their supersession ancestor closure. Output names include actual baseline names. Baseline-v3 compares against baseline-v1 and baseline-v2 without combining runs, usage, patches, time, or infrastructure histories.

## Exclusions

No case/repository hardcoding, evaluator metadata routing, Oracle/gold/private data, additional model, multi-agent flow, memory bank, network service, dependency installation, new main-loop budget, hidden-test selection, or baseline-v1/v2 modification.
