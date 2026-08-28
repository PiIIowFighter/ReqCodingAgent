# Iteration 1 Reproducible SWE-bench Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and actually validate a frozen, reproducible, resumable, isolated SWE-bench evaluation environment for the specified 15 paired cases.

**Architecture:** A Python 3.11 package owns locking, strict schemas, data validation, isolation, replay orchestration, integrity-preserving recovery, and reporting. It delegates test execution to an exact external checkout of the official SWE-bench harness and stores all large/private runtime products under ignored external caches or `artifacts/`.

**Tech Stack:** Python 3.11, pathlib, argparse, jsonschema, pytest, Git, Ubuntu WSL2, Docker Desktop Linux engine, official SWE-bench at commit `7a21e05772954cc81471ae19d56f436cecf43c54`.

## Global Constraints

- Do not modify, move, or delete existing files under `资料/` or `计划/`.
- Do not implement a Coding Agent, LLM calls, clarification, OntoAgent algorithms, or E1-E4.
- Freeze exactly 12 test pairs and 3 dev pairs with the specified prompts, Oracle facts, source hashes, upstream commits, and scoring.
- Use external configurable caches; never create a nested Git repository beneath the project root.
- Validate all three source locks at runtime and prove all 15 IDs belong to both fixed Verified and fixed Lite.
- Use pathlib and subprocess argument arrays; support Windows WSL2 and Chinese/space paths.
- no-op and gold verdicts come from every FAIL_TO_PASS/PASS_TO_PASS result, not exit code or aggregate resolved.
- Recovery requires matching input fingerprint, completion marker, strict result schema, and verified key-artifact hashes.
- Timeout kills the subprocess group and cleans only Docker resources labelled with the current run_id; global prune is forbidden.
- Isolation proof must execute workspace construction and positive/negative probes, including a container probe.
- Preflight must perform a real Docker bind-mount read/write test with a Chinese and space-containing path.
- Never commit datasets, checkouts, caches, images, large logs, secrets, absolute user paths, `.env`, or `资料/`.
- Create and normally push a focused checkpoint only after its scoped tests pass and immutable evidence is saved; do not claim iteration completion until every Section 19 criterion and all 30 replays pass.

## File Map

- `.gitignore`: protected and generated-content exclusions.
- `.env.example`: optional cache/timeouts only, no credentials.
- `pyproject.toml`: Python 3.11 package, runtime/test dependencies, pytest configuration.
- `README.txt`: under 1000 Chinese characters; repository, commands, features, WSL2/Docker.
- `benchmark/source-lock.json`: exact three upstream revisions and URLs.
- `benchmark/manifests/case-definitions.json`: frozen 15 case metadata and deterministic fuzzy transformation specifications.
- `benchmark/manifests/paired-cases.jsonl`: generated full/fuzzy public records.
- `benchmark/private/oracles.jsonl`: evaluator-only frozen Oracle records.
- `benchmark/schemas/*.schema.json`: strict source-lock, case, Oracle, replay, completion, summary schemas.
- `src/evalsys/config.py`: project/external cache/artifact path resolution.
- `src/evalsys/errors.py`: actionable typed errors and CLI formatting.
- `src/evalsys/process.py`: argument-array execution, WSL bridge, process-group timeout.
- `src/evalsys/locks.py`: exact external Git/data revision verification.
- `src/evalsys/schema.py`: JSON/JSONL strict validation.
- `src/evalsys/frozen_cases.py`: allowlist, source hashes, fuzzy transformation engine.
- `src/evalsys/data.py`: fixed Verified/Lite acquisition, membership and manifest preparation.
- `src/evalsys/validation.py`: cross-record counts, distributions, pairs, evidence and hash checks.
- `src/evalsys/preflight.py`: dependency and real bind-mount checks.
- `src/evalsys/isolation.py`: executable Agent workspace construction and negative probes.
- `src/evalsys/harness.py`: exact official harness adapter and raw per-test extraction.
- `src/evalsys/verdict.py`: per-test no-op/gold verdict calculation.
- `src/evalsys/recovery.py`: fingerprints, atomic outputs, completion markers and artifact hash verification.
- `src/evalsys/replay.py`: per-case/all-case orchestration, statuses, labels and targeted cleanup.
- `src/evalsys/reporting.py`: machine result, Markdown matrix and sanitized audit outputs.
- `src/evalsys/acceptance.py`: Section 19 gate, repository hygiene and README checks.
- `src/evalsys/cli.py`: seven required commands.
- `scripts/validate-all.ps1`, `scripts/validate-all.sh`: quoting-safe wrappers.
- `tests/`: unit and integration tests with small fixtures; no upstream datasets.
- `audit/iteration1/`: generated sanitized public audit outputs after real validation.

---

### Task 1: Establish repository safety and Python package

**Files:**
- Create: `.gitignore`, `.env.example`, `pyproject.toml`, `src/evalsys/__init__.py`, `src/evalsys/errors.py`, `src/evalsys/config.py`
- Test: `tests/test_repository_layout.py`, `tests/test_config.py`

**Interfaces:**
- Produces: `Settings.from_env(project_root: Path) -> Settings`; `EvalError(message, hint, category)`.

- [ ] Capture top-level file mtimes/hashes for protected files and initialize Git at the root on `main`; add `origin` only after re-running empty-remote verification.
- [ ] Write failing tests asserting `资料/`, `.env`, `artifacts/`, caches and nested upstream checkouts are ignored, and that external cache resolution rejects any path inside `project_root`.
- [ ] Run `py -3.11 -m pytest tests/test_repository_layout.py tests/test_config.py -v`; expect failures because package/config do not exist.
- [ ] Implement the minimal package/config with `Path.resolve()` containment checks, environment variables `EVALSYS_CACHE_ROOT`, `EVALSYS_ARTIFACT_ROOT`, and no secret values.
- [ ] Run the focused tests; expect all pass.
- [ ] Re-hash protected files and assert unchanged. Commit only if this is a coherent checkpoint with saved scoped-test evidence.

### Task 2: Define strict schemas and frozen source locks

**Files:**
- Create: `benchmark/source-lock.json`, `benchmark/schemas/source-lock.schema.json`, `benchmark/schemas/public-case.schema.json`, `benchmark/schemas/oracle.schema.json`, `benchmark/schemas/replay-result.schema.json`, `benchmark/schemas/completion.schema.json`, `benchmark/schemas/validation-summary.schema.json`, `src/evalsys/schema.py`
- Test: `tests/test_schema.py`, `tests/fixtures/schema/`

**Interfaces:**
- Produces: `load_schema(name: str) -> dict`; `validate_json(value, schema_name) -> None`; `validate_jsonl(path, schema_name) -> list[dict]`.

- [ ] Write tests that valid minimal records pass while unknown fields, invalid enums, absent required fields, and malformed JSONL fail with an actionable `EvalError`.
- [ ] Run focused tests; expect failures.
- [ ] Add schemas with `additionalProperties: false`, fixed enums and exact 40/64-hex patterns; implement validators.
- [ ] Run focused tests; expect pass. Commit only if this is a coherent checkpoint with saved scoped-test evidence.

### Task 3: Verify external SWE-bench checkout and all three locks

**Files:**
- Create: `src/evalsys/process.py`, `src/evalsys/locks.py`
- Test: `tests/test_process.py`, `tests/test_locks.py`

**Interfaces:**
- Produces: `run_checked(argv: Sequence[str], *, cwd: Path | None, timeout_s: int, env: Mapping[str,str] | None) -> CommandResult`; `ensure_harness_checkout(settings, lock) -> Path`; `verify_source_locks(settings) -> LockVerification`.

- [ ] Write tests using temporary Git repositories to reject a checkout under project root, dirty/wrong HEAD, symbolic branch drift, and any mismatch among the three locks.
- [ ] Add a process timeout test whose child spawns a grandchild; assert both processes terminate.
- [ ] Run focused tests; expect failures.
- [ ] Implement external clone/fetch/checkout by argument arrays, verify `git rev-parse HEAD` exactly equals the lock, and record all three lock checks. Use a new process group/session and kill the full group on timeout.
- [ ] Run focused tests; expect pass. Commit only if this is a coherent checkpoint with saved scoped-test evidence.

### Task 4: Encode the immutable 15-case definitions and fuzzy transformations

**Files:**
- Create: `benchmark/manifests/case-definitions.json`, `src/evalsys/frozen_cases.py`
- Test: `tests/test_frozen_cases.py`, `tests/fixtures/prompts/`

**Interfaces:**
- Produces: `CASE_IDS`; `transform_prompt(instance_id: str, original: str) -> str`; `build_public_records(source_rows) -> list[dict]`; `build_oracles() -> list[dict]`.

- [ ] Write tests for exact allowlist, 12/3 split, 4/4/4 and 1/1/1 distribution, exact base commits/source hashes, exact frozen Oracle fields, and every specified textual transformation.
- [ ] Run focused tests; expect failures.
- [ ] Encode all Section 7, 9 and 10 records and deterministic transformations without using patch/test_patch/hints.
- [ ] Run focused tests; expect pass. Commit only if this is a coherent checkpoint with saved scoped-test evidence.

### Task 5: Acquire fixed Verified and Lite and prepare manifests

**Files:**
- Create: `src/evalsys/data.py`, generated `benchmark/manifests/paired-cases.jsonl`, `benchmark/private/oracles.jsonl`
- Test: `tests/test_data.py`

**Interfaces:**
- Produces: `prepare_data(settings) -> PreparedData`; `load_fixed_verified(settings) -> dict[str, SourceTask]`; `load_fixed_lite_ids(settings) -> set[str]`.

- [ ] Write fixture-driven tests that preparation fails if an ID is absent from Lite, a Verified prompt hash differs, source revisions are unverified, or source fields differ across a pair.
- [ ] Run focused tests; expect failures.
- [ ] Implement fixed-revision downloads/cache verification, explicitly loading both datasets and retaining proof of 15-ID intersection; extract Verified rows and generate manifests atomically.
- [ ] Run focused tests; expect pass.
- [ ] Execute real `prepare-data` against fixed revisions; verify all 15 official prompt hashes before accepting generated files. Commit only if this is a coherent checkpoint with saved scoped-test evidence.

### Task 6: Implement complete cross-record validation

**Files:**
- Create: `src/evalsys/validation.py`
- Test: `tests/test_validation.py`

**Interfaces:**
- Produces: `validate_benchmark(prepared: PreparedData) -> ValidationReport`.

- [ ] Write one failing test for each Section 18 item 1-8 and 10: counts, distributions, allowlist/unique pair, pair equality including F2P/P2P, verbatim full hash, fuzzy metadata/freeze, evidence substring/provenance, nullable ontology, strict outputs.
- [ ] Run focused tests; expect failures.
- [ ] Implement validations with stable issue codes and actionable messages.
- [ ] Run focused tests and then real `validate-data`; expect pass. Commit only if this is a coherent checkpoint with saved scoped-test evidence.

### Task 7: Implement preflight with real Chinese/space Docker bind mount

**Files:**
- Create: `src/evalsys/preflight.py`
- Test: `tests/test_preflight.py`

**Interfaces:**
- Produces: `run_preflight(settings, *, perform_bind_test: bool = True) -> PreflightReport`.

- [ ] Write tests for missing Docker, non-Linux engine, missing WSL2, wrong Python, unwritable external cache, failed source locks, and bind test failures; assert no replay success can be emitted.
- [ ] Write an integration test creating `中文 路径`, placing a sentinel, running a small pinned image with bind mount, reading sentinel and writing an acknowledgement back.
- [ ] Run focused tests; expect failures.
- [ ] Implement checks and the actual bind read/write test via argument arrays/WSL path conversion; remove only the test container/temp directory.
- [ ] Run unit tests and real `preflight`; record Docker/Python/harness versions. Commit only if this is a coherent checkpoint with saved scoped-test evidence.

### Task 8: Build executable Agent/evaluator isolation

**Files:**
- Create: `src/evalsys/isolation.py`
- Test: `tests/test_isolation.py`, `tests/fixtures/isolation/`

**Interfaces:**
- Produces: `construct_agent_workspace(task_repo: Path, public_case: dict, destination: Path) -> WorkspaceManifest`; `prove_isolation(settings) -> IsolationProof`.

- [ ] Write tests that seed canaries in Oracle, patches, hints, plan, materials and evaluator logs; construct a workspace and assert allowed repo/prompt visible but every canary/path absent.
- [ ] Add a Docker negative-probe test that mounts only the constructed workspace and attempts to access prohibited paths/content; require all negative probes to fail and positive probes to pass.
- [ ] Run focused tests; expect failures.
- [ ] Implement copy/mount construction without project-root mount and executable host/container probes; emit sanitized evidence.
- [ ] Run focused tests and real proof. Commit only if this is a coherent checkpoint with saved scoped-test evidence.

### Task 9: Adapt official harness and calculate per-test verdicts

**Files:**
- Create: `src/evalsys/harness.py`, `src/evalsys/verdict.py`
- Test: `tests/test_harness.py`, `tests/test_verdict.py`, `tests/fixtures/harness-results/`

**Interfaces:**
- Produces: `HarnessRunner.run(task, mode, run_context) -> RawHarnessResult`; `extract_test_outcomes(raw, expected_f2p, expected_p2p) -> TestOutcomeSet`; `decide_verdict(mode, outcomes) -> Verdict`.

- [ ] Write fixture tests proving empty/no prediction cannot be treated as successful no-op unless test execution markers and every expected test outcome exist.
- [ ] Write exhaustive verdict tests: no-op requires each F2P fail and P2P pass; gold requires all pass; missing/duplicate/unparseable tests yield `invalid`; harness/environment failures yield `infra_failed`.
- [ ] Run focused tests; expect failures.
- [ ] Implement the exact official harness command at pinned checkout, a no-op execution route that truly runs tests, raw output retention, and per-test extraction independent of exit code/top-level resolved.
- [ ] Run focused tests; expect pass. Commit only if this is a coherent checkpoint with saved scoped-test evidence.

### Task 10: Implement integrity-checked recovery and atomic logs

**Files:**
- Create: `src/evalsys/recovery.py`
- Test: `tests/test_recovery.py`

**Interfaces:**
- Produces: `compute_input_fingerprint(input: ReplayInput) -> str`; `write_completed_run(result, artifacts) -> CompletionMarker`; `load_reusable_run(directory, expected_fingerprint) -> ReplayResult | None`.

- [ ] Write tests rejecting reuse for absent marker, schema failure, mismatched fingerprint, missing artifact, altered artifact hash, temporary/incomplete file, and nonterminal result.
- [ ] Run focused tests; expect failures.
- [ ] Implement canonical JSON hashing, atomic file replacement and completion markers containing key relative paths/hashes.
- [ ] Run focused tests; expect pass. Commit only if this is a coherent checkpoint with saved scoped-test evidence.

### Task 11: Orchestrate replay, timeout and targeted Docker cleanup

**Files:**
- Create: `src/evalsys/replay.py`
- Test: `tests/test_replay.py`, `tests/test_cleanup.py`

**Interfaces:**
- Produces: `replay_cases(settings, cases, mode, *, timeout_s, workers, resume) -> RunDirectory`; `cleanup_run_resources(run_id: str) -> CleanupReport`.

- [ ] Write orchestration tests for status classification, serial default, low explicit concurrency, resume, timeout, logs, retry history, and run_id labels.
- [ ] Seed labelled/unlabelled fake Docker resources; assert cleanup selects only `evalsys.run_id=<run_id>` and never invokes prune or unfiltered removal.
- [ ] Run focused tests; expect failures.
- [ ] Implement UUID run IDs, labelled official harness invocation, process-group timeout, targeted Docker `--filter label=...` enumeration/removal and structured retry records.
- [ ] Run focused tests; expect pass. Commit only if this is a coherent checkpoint with saved scoped-test evidence.

### Task 12: Generate reports and sanitized audit artifacts

**Files:**
- Create: `src/evalsys/reporting.py`, `audit/iteration1/.gitkeep`
- Test: `tests/test_reporting.py`, `tests/test_sanitization.py`

**Interfaces:**
- Produces: `generate_report(run_directory: Path) -> ReportPaths`; `publish_audit(run_directory: Path, test_summary: TestSummary, destination: Path) -> AuditPaths`.

- [ ] Write tests for JSON/JSONL, Markdown matrix with all 15 IDs, failed-stage summary, run manifest hashes, and audit generation only from schema-valid actual results.
- [ ] Add sanitization tests for Windows/WSL absolute paths, API/SSH-like secrets, environment dumps and oversized raw logs.
- [ ] Run focused tests; expect failures.
- [ ] Implement deterministic reports and sanitized public summaries with relative paths only.
- [ ] Run focused tests; expect pass. Commit only if this is a coherent checkpoint with saved scoped-test evidence.

### Task 13: Add CLI and one-command validate-all

**Files:**
- Create: `src/evalsys/cli.py`, `scripts/validate-all.ps1`, `scripts/validate-all.sh`
- Test: `tests/test_cli.py`, `tests/test_validate_all.py`

**Interfaces:**
- Produces CLI commands `preflight`, `prepare-data`, `validate-data`, `replay`, `validate-all`, `report` with nonzero actionable failure exits.

- [ ] Write CLI tests for all documented commands, split/mode/timeout/workers/resume options, stage ordering, failure propagation and result directory output.
- [ ] Run focused tests; expect failures.
- [ ] Implement argparse entry point and validate-all pipeline: preflight → all locks → data → validation → no-op → gold → report → isolation → acceptance inputs.
- [ ] Implement quoting-safe PowerShell/Bash wrappers using direct argument arrays/quoted positional expansion.
- [ ] Run focused tests and help commands; expect pass. Commit only if this is a coherent checkpoint with saved scoped-test evidence.

### Task 14: Implement Section 19 acceptance gate and documentation

**Files:**
- Create: `src/evalsys/acceptance.py`, `README.txt`
- Test: `tests/test_acceptance.py`, `tests/test_readme.py`, `tests/test_git_hygiene.py`

**Interfaces:**
- Produces: `evaluate_acceptance(project_root, validation_run, unit_test_record) -> AcceptanceReport`; process exit 0 only when every Section 19 checkbox is true.

- [ ] Write tests mapping every Section 19 criterion to an explicit acceptance key; reject missing replay, failures, absent audit, tracked `资料`, secrets, caches, large logs, dirty protected files, wrong origin, README over 1000 characters, or missing isolation proof.
- [ ] Run focused tests; expect failures.
- [ ] Implement acceptance gate, staged/tracked file scanning and README character counting; write concise README with URL, commands, stage features and WSL2/Docker instructions.
- [ ] Run focused tests; expect pass. Commit only if this is a coherent checkpoint with saved scoped-test evidence.

### Task 15: Review, unit-test and verify the runtime surface

**Files:**
- Modify only implementation/test files identified by review; never protected files.

**Interfaces:**
- Consumes all prior interfaces.
- Produces verified implementation ready for real 30-replay validation.

- [ ] Run `py -3.11 -m pytest -ra -v`; require zero failures/skips for mandatory tests.
- [ ] Run static repository scans for absolute paths, secrets, nested `.git`, forbidden subprocess shell strings and global Docker prune/removal.
- [ ] Invoke code review and simplification review; address only verified in-scope findings with focused regression tests.
- [ ] Run end-to-end verification of preflight, prepare-data, validate-data, report, isolation and a controlled replay path.
- [ ] Re-run full unit suite and record exact command/output in local artifacts. Commit only if this is a coherent checkpoint with saved scoped-test evidence.

### Task 16: Run the actual 15-task no-op and gold validation

**Files:**
- Generate ignored: `artifacts/<validate-all-run>/...`
- Generate tracked only after success: `audit/iteration1/validation-summary.json`, `audit/iteration1/noop-gold-matrix.md`, `audit/iteration1/run-manifest.json`, `audit/iteration1/test-summary.txt`, `audit/iteration1/isolation-proof.json`

**Interfaces:**
- Produces one schema-valid validate-all result directory and public audit derived from it.

- [ ] Run `py -3.11 -m evalsys.cli validate-all --resume --workers 1` from the project root with the external cache.
- [ ] Observe all 15 no-op runs genuinely execute tests and satisfy each F2P/P2P expectation.
- [ ] Observe all 15 gold runs genuinely execute tests and satisfy each F2P/P2P expectation.
- [ ] On infra/test instability, retain logs, classify, retry only under recorded deterministic policy, never replace a task, and rerun with resume.
- [ ] If any final status is not passed, stop before commit/push and report the blocking matrix.
- [ ] If all pass, generate audit files from the actual result directory and verify their schemas/hashes/sanitization.

### Task 17: Final acceptance, completion evidence, push and remote verification

**Files:**
- Stage only approved iteration-1 source/test/docs/audit files and the unchanged implementation specification under `计划/`.

**Interfaces:**
- Produces the iteration-completion evidence commit and verified matching remote main.

- [ ] Re-run full unit tests and validate-all acceptance against the retained actual result directory.
- [ ] Verify all Section 19 keys true, README count ≤1000, protected hashes unchanged, no nested repo, `资料/` untracked/ignored, and Git has no secrets/caches/data/logs/absolute paths.
- [ ] Inspect `git diff --cached --name-status` before commit; remove anything outside scope.
- [ ] Create exactly one commit with message `feat(eval): establish frozen reproducible SWE-bench benchmark` and required co-author trailer.
- [ ] Push with `git push -u origin main`; never force.
- [ ] Compare `git rev-parse HEAD` with `git ls-remote origin refs/heads/main`; require exact match.
- [ ] Capture final status, change list, tests, validate-all directory, audit path, 15×2 matrix, retries/failures, README character count and executable isolation proof for handoff.

## Requirement Traceability

- Specification Section 18.1-8,10: Tasks 4-6 and strict schemas in Task 2.
- Section 18.9 isolation: Task 8 executable construction and negative probes.
- Section 18.11 Docker blocking: Task 7.
- Section 18.12 actual audit provenance: Tasks 12 and 16.
- Section 19 data/schema/hash/pair distribution: Tasks 4-6, 14, 17.
- Section 19 no-op/gold and no replacement: Tasks 9, 11, 16.
- Section 19 recovery/log/report: Tasks 10-13, 16.
- Section 19 README/Git hygiene/origin/audit: Tasks 1, 12, 14, 17.
- User constraint 1 external checkout: Tasks 1 and 3.
- User constraint 2 Lite proof/all locks: Tasks 3 and 5.
- User constraint 3 actual per-test replay: Tasks 9 and 16.
- User constraint 4 hardened recovery/timeout/cleanup: Tasks 3, 10 and 11.
- User constraint 5 executable isolation and bind mount: Tasks 7 and 8.
