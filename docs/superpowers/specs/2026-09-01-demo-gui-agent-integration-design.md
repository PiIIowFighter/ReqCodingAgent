# Demo GUI Coding Agent Integration Design

## Scope

Connect the existing `demo_gui` workspace to the existing ReqCodingAgent implementation without changing the Agent's decision logic, requirement ontology, frozen baselines, prompts, audit evidence, or benchmark data.

Development order:

1. GUI–Coding Agent integration
2. Agent trace visualization
3. Ontology visualization as an independent presentation layer

The current frozen ontology remains unchanged.

## User-visible navigation

The left-bottom `Settings` entry remains the single settings entry. The duplicate top-right settings icon is removed. Settings pages include a `Back to workspace` control that returns to `/`.

## Startup and workspace boundary

The server starts with an explicit clean Git workspace:

```sh
python demo_gui/server.py --workspace <clean-git-repository>
```

It uses `configs/agent/live-local-proxy.json` by default, with an optional server-side `--config` override. Workspace and config paths are never accepted from browser requests. The browser receives only the workspace directory name, never an absolute path.

At startup the server verifies that the workspace exists, is a Git repository, and is clean. A failed check prevents Agent execution and is reported without exposing the path.

## Agent execution architecture

The GUI server launches the repository-native command as a background child process:

```sh
python -m reqagent.cli run --workspace <workspace> --task <task> --config <config> --artifact-root <GUI-owned artifact root>
```

This reuses the existing model adapter, refinement flow, tool registry, isolated workspace, budgets, command container, checkpoint logic, and patch collection. The GUI does not call the provider directly and does not implement a second Agent loop.

Only one Agent task may run at a time. New submissions while one is active receive a conflict response.

## Local API

The loopback-only server adds:

- `GET /api/runtime`
- `POST /api/tasks`
- `GET /api/tasks/{task_id}`
- `GET /api/tasks/{task_id}/events?after=<offset>`
- `GET /api/tasks/{task_id}/patch`
- `GET /api/tasks/{task_id}/patch/download`

`POST /api/tasks` accepts only a bounded JSON object containing a non-empty `task` string. It does not accept commands, paths, configuration, model identifiers, or artifact locations.

Mutating requests require JSON content type and a same-origin `Origin` when that header is present. No general file-read, command, environment, or process API is exposed.

## Task lifecycle

1. The browser submits a task.
2. The server creates an opaque task ID and a GUI-owned artifact directory.
3. A background thread starts the ReqCodingAgent child process.
4. The server incrementally reads the run's `events.jsonl` and sanitizes events for display.
5. The browser polls for new events and task status.
6. At completion, the server reads the Agent result and generated patch.
7. The GUI presents the final summary, stop reason, trace, and patch.

The original workspace is never modified by the GUI. The generated patch may only be previewed or downloaded. There is no apply endpoint or apply button.

## Trace presentation

The main view changes from the empty composer to a task timeline while a run is active. It displays:

- user task
- queued/running/completed/failed status
- refinement stage transitions where available
- model response boundaries without hidden reasoning text
- tool name, phase, success/failure, and a short sanitized result summary
- final summary and stop reason
- patch statistics and unified diff

It does not display tokens, provider endpoints, credentials, absolute paths, full environment values, benchmark results, or hidden model reasoning.

The trace UI is a presentation adapter over existing run events. It does not alter Agent events or decision behavior.

## Ontology visualization

The existing ontology explorer remains a separate settings presentation layer. Its tree continues to load dynamically from `configs/frozen/baseline-v3/requirement-ontology.json`, verify the SHA recorded by baseline-v3, and combine it with `demo_gui/ontology_annotations.json` only for presentation.

No category, slot, status, selection rule, or requirement-refinement behavior is added or changed.

## Failure and cleanup behavior

- Invalid task input returns a bounded 400 response.
- Concurrent task submission returns 409.
- Agent process launch or runtime failure becomes a failed task with a sanitized message.
- Missing or malformed trace files do not crash the server; the task reports unavailable trace data.
- Server shutdown terminates an active child process and waits briefly for cleanup.
- Completed task metadata remains in memory for the server lifetime; no database or browser credential storage is added.

## Minimal verification policy

Do not run benchmark, development, formal, Docker evaluation, or the full test suite.

Verification is limited to:

1. server startup
2. GUI page load
3. one simple Agent execution using the existing scripted configuration for deterministic automated smoke coverage
4. patch generation, preview, and download
5. one manually authorized live Agent smoke only if current provider and container prerequisites are available

Also run `py_compile` on changed Python files and `git diff --check`. No new third-party dependency is introduced.
