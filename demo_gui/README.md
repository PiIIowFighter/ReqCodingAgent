# ReqCodingAgent local demo GUI

A dependency-free local workspace for running the repository-native Coding Agent, viewing its sanitized execution trace, previewing or downloading the generated patch, and browsing the frozen Requirement Ontology.

## Start

The workspace must be an existing clean Git repository:

```sh
python demo_gui/server.py --workspace /path/to/clean/repository
```

The default runtime configuration is `configs/agent/live-local-proxy.json`. For a deterministic offline run:

```sh
python demo_gui/server.py --workspace /path/to/clean/repository --config configs/agent/offline-scripted.json
```

Open `http://127.0.0.1:8765/`. The server binds only to loopback.

## Safety boundary

- Workspace and configuration paths are fixed at startup and never accepted from browser requests.
- The Agent runs in an isolated copy. The source repository is not modified.
- The UI previews and downloads patches; it has no apply endpoint.
- Trace events expose phases, tool outcomes, and bounded summaries, not credentials, provider metadata, token usage, absolute paths, or hidden reasoning.
- Only one task runs at a time.

The ontology page independently verifies its SHA-256 against frozen `baseline-v3` metadata before displaying the tree.
