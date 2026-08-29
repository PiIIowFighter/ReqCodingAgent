# Iteration 2 Coding Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the complete offline-verifiable baseline Coding Agent implementation checkpoint without running any live model, SWE-bench development, or formal evaluation.

**Architecture:** `reqagent` is a provider-neutral package that owns protocol types, configuration, six local tools, workspace isolation, context, checkpoints, the autonomous loop, patch collection, artifacts, and CLI. `evalsys` may invoke `reqagent` through a narrow handoff module, while protected benchmark entry points remain closed until a confirmed live configuration and frozen baseline exist.

**Tech Stack:** Python 3.11 standard library, existing `jsonschema`, Git CLI via argument arrays, pytest.

## Global Constraints

- Preserve dependency direction `evalsys -> reqagent`; `reqagent` never imports `evalsys`, SWE-bench, Oracle data, or instance identities.
- Implement exactly six tools: `list_files`, `read_file`, `search_text`, `apply_patch`, `run_command`, `submit`.
- Do not use Agent frameworks, hosted file/terminal/code tools, `shell=True`, or real model calls.
- Reject absolute paths, traversal, `.git`, symlink/realpath escape, protected paths, binary/oversized patches, and unsafe command environments.
- Keep all frozen `计划/` files and `资料/` unchanged.
- Do not run any SWE-bench task, E1/E2, `baseline-v1` freeze, formal run, Docker pull/build, or Iteration 1 replay/validation.
- Tests are risk-prioritized; run one final `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -m "not integration"` only after implementation.
- The final state is an “Iteration 2 implementation checkpoint,” not Iteration 2 acceptance.

---

### Task 1: Model protocol and strict configuration

**Files:**
- Create: `src/reqagent/__init__.py`, `src/reqagent/model.py`, `src/reqagent/config.py`
- Create: `configs/agent/offline-scripted.json`, `configs/agent/live-template.json`
- Test: `tests/agent/test_model_config.py`

**Interfaces:**
- Produces immutable `ModelMessage`, `ToolDefinition`, `ModelRequest`, `NormalizedToolCall`, `ModelResponse`, `ModelError`, `ModelAdapter`, and `ScriptedModel`.
- Produces `AgentConfig.load(path)`, `validate(live=False)`, `public_dict()`, and canonical configuration hashing.

- [ ] Define strict dataclasses and JSON-compatible serialization; reject unknown roles, finish reasons, usage fields, call IDs, tool names, and non-object arguments.
- [ ] Validate JSON config recursively with exact keys, explicit environment-variable references, placeholder detection, positive bounded budgets, and redaction of secret values and URL queries.
- [ ] Implement deterministic scripted responses and explicit unsupported-live behavior; never import or call a provider SDK.
- [ ] Add focused tests for valid/invalid calls, unknown fields, missing environment variables, placeholders, and redaction.
- [ ] Run `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/agent/test_model_config.py -q` and retain the exact result.

### Task 2: Workspace, patch policy, and six protected tools

**Files:**
- Create: `src/reqagent/workspace.py`, `src/reqagent/patching.py`
- Create: `src/reqagent/tools/{__init__,base,files,search,patch,command,submit}.py`
- Test: `tests/agent/test_tools.py`, `tests/agent/test_workspace.py`

**Interfaces:**
- Consumes `ToolDefinition` and config limits.
- Produces `WorkspacePolicy`, `GitWorkspace`, `PatchResult`, `ToolEnvelope`, `ToolRegistry.execute(name, arguments)`, and six deterministic schemas.

- [ ] Implement canonical relative-path validation before and after resolution, reserved/protected-path checks, symlink escape detection, binary/size checks, and stable sorting.
- [ ] Implement detached disposable worktree creation from a clean source repository and cleanup metadata without editing the source checkout.
- [ ] Implement `list_files`, numbered `read_file`, and deterministic Python search with `rg` argument-array acceleration when available.
- [ ] Implement atomic patch application in a temporary copied tree, pre/post policy checks, then copy only validated changed files into the worktree.
- [ ] Implement command execution as an argument-array host process using `bash -lc` only inside an explicitly isolated runtime; scrub environment, use process groups, enforce timeout, preserve nonzero exits as evidence, and deterministically retain output head/tail.
- [ ] Implement `submit` as metadata only, cross-checking claimed tests against command history.
- [ ] Collect normalized `git diff --binary=no` output, SHA-256, file/add/delete counts, and enforce 5 files/500 changed lines/128 KiB/no binary/protected-path limits.
- [ ] Add focused traversal, `.git`, symlink, atomic-failure, patch-limit, success/nonzero/truncation/timeout tests.
- [ ] Run the two focused test files and retain the exact result.

### Task 3: Context, artifacts, checkpoint, and resume

**Files:**
- Create: `src/reqagent/context.py`, `src/reqagent/checkpoint.py`, `src/reqagent/trace.py`
- Test: `tests/agent/test_context_checkpoint.py`

**Interfaces:**
- Produces `ContextLedger`, `ContextSummary`, `RunStore`, `CheckpointStore.save/load`, canonical checksums, and workspace/config/code/prompt/schema/task fingerprints.

- [ ] Keep system prompt and original task immutable; truncate tool output before history insertion and estimate tokens deterministically.
- [ ] At the configured threshold, replace only old interactions with a fixed-schema summary retaining inspected files, findings, modifications, commands/verdicts, open issues, next actions, no-progress actions, and diff fingerprint; preserve two recent complete rounds.
- [ ] Create unique artifact directories, append JSONL events, write JSON atomically with fsync/replace, checksum every checkpoint payload, and update `LATEST` only after verification.
- [ ] Save a checkpoint after each complete model response and each tool result with source-code, config, prompts, schemas, task, budget, base commit, diff, and protected fingerprint hashes.
- [ ] Reject resume for `COMPLETE`, bad checksum/schema, changed code/config/prompt/schema/task/workspace, looser budgets, or invalid state; never create `COMPLETE` until final result, patch, and checksums exist.
- [ ] Test compression invariants, one valid resume, checksum mismatch, workspace mismatch, completion refusal, and budget-loosening refusal.

### Task 4: Autonomous agent loop

**Files:**
- Create: `src/reqagent/loop.py`
- Test: `tests/agent/test_loop.py`

**Interfaces:**
- Consumes adapter, registry, workspace, context, checkpoints, and budgets.
- Produces `AgentLoop.run()` / `.resume()` and `AgentResult` with stop reason, trace, usage, submit metadata, warnings, and best patch.

- [ ] Implement explicit Prepare → CallModel → Parse → Execute → Checkpoint → CollectPatch transitions with monotonic event/model/tool sequence numbers.
- [ ] Classify model errors, retry only network/timeout/429/5xx up to the configured bound without resetting wall time, and map refusals/unrecoverable errors precisely.
- [ ] Validate every tool call before execution, execute multiple calls in response order, and stop at `submit` without executing later calls.
- [ ] Enforce step/tool/wall-clock/invalid-output budgets and normalized repeated-action fingerprints that include pre-action diff and result summary; warn once, stop on the next unchanged repeat.
- [ ] On every exit or exception, attempt best-patch collection and write a truthful result; `submitted` never means tests passed.
- [ ] Test normal multi-round scripted flow, invalid calls, submit ordering, budgets, repeats, retries, and patch collection on failures.

### Task 5: CLI, frozen prompt, and offline run evidence

**Files:**
- Create: `src/reqagent/cli.py`, `prompts/baseline/system.txt`, `prompts/baseline/protocol.txt`
- Modify: `pyproject.toml`
- Test: `tests/agent/test_cli_e2e.py`

**Interfaces:**
- Produces `reqagent doctor --config ... [--live]`, `reqagent run --workspace ... (--task ...|--task-file ...) --config ...`, and `reqagent resume --run-id ...`.

- [ ] Save specification section 8 system prompt byte-for-byte; keep the protocol suffix neutral and free of benchmark variants/private evaluator hints.
- [ ] Add the console entry point and argparse help; support Unicode/space paths through `pathlib` and argument arrays.
- [ ] Make `doctor` validate offline config and make `--live` fail clearly while placeholders/protocol implementation remain unconfirmed, without issuing network traffic.
- [ ] Make `run` create a disposable workspace and write machine JSON while stdout stays human-readable; an unresolved agent run remains a valid recorded result.
- [ ] Make `resume` locate and validate an existing run, restore scripted-model position and loop state, and continue only from a legal checkpoint.
- [ ] Test all help commands and a temporary Unicode/space Git repository through search → patch → command → submit → normalized patch, then apply that patch to a fresh clone.

### Task 6: evalsys handoff and protected future entry points

**Files:**
- Create: `src/evalsys/agent_runner.py`, `src/evalsys/baseline.py`
- Modify: `src/evalsys/cli.py`
- Test: `tests/agent/test_evalsys_handoff.py`

**Interfaces:**
- Produces a public-task-only `AgentRunRequest`, result/patch handoff, configuration preflight, and closed `agent-run`, `run-dev`, `freeze-baseline`, `run-formal` entry points.

- [ ] Build requests only from projected task text/repository/base commit; never include variant, ambiguity, Oracle, hints, gold/test patches, or private paths in `reqagent` inputs.
- [ ] Validate that `reqagent` imports without `evalsys`, while `evalsys.agent_runner` imports `reqagent`.
- [ ] Refuse formal/development execution unless explicit confirmation, non-placeholder live config, and required frozen-baseline evidence exist; this checkpoint supplies no bypass.
- [ ] Add synthetic fixtures proving identity isolation and gate behavior without opening frozen task manifests.

### Task 7: Documentation, final verification, audit, and Git delivery

**Files:**
- Modify: `README.txt`
- Create: `audit/iteration2/index.json`, `audit/iteration2/runs/<offline-run-id>/*`
- Test: existing and new non-integration tests

**Interfaces:**
- Produces one truthful, append-only, redacted Iteration 2 development audit record.

- [ ] Update README within 1000 Chinese characters with standalone CLI, scripted offline mode, safety boundaries, and explicit live/evaluation deferral.
- [ ] Run targeted suites once per coherent implementation boundary; fix only observed defects.
- [ ] Invoke project runtime verification and code review before committing source changes.
- [ ] Run final `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -m "not integration"`, one scripted temporary-repository E2E, and `git diff --check`.
- [ ] Scan tracked/untracked changes for secret-shaped values, user absolute paths, files over the chosen repository threshold, `资料/`, raw artifacts, `.env`, `.claude`, task workspaces, and caches; count README characters.
- [ ] Generate one non-overwriting redacted audit summary from the real offline E2E artifact, including run ID, stop reason, tool trace summary, patch hash/stats, exact command results, and deferred list.
- [ ] Create honest responsibility-based commits, each ending with the required co-author trailer; push normally to `origin/main` and verify local HEAD equals remote main after every push.
- [ ] Report exact commands, exit codes, test counts, commits/hashes, E2E evidence, safety boundaries, unverified live fields, all deferred evaluations, dependency/Docker activity, scans, README count, and final status; call it only “Iteration 2 implementation checkpoint.”
