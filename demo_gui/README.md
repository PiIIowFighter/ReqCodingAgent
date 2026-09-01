# ReqCodingAgent local demo GUI

A dependency-free, read-only desktop workspace and Requirement Ontology viewer.

## Start

```sh
python demo_gui/server.py
```

Open:

- `http://127.0.0.1:8765/`
- `http://127.0.0.1:8765/settings/ontology`

The server binds only to `127.0.0.1`. It verifies the ontology SHA-256 against the frozen `baseline-v3` metadata before exposing the tree, serves an explicit static allowlist, and provides only two read-only APIs:

- `GET /api/health`
- `GET /api/ontology`

This stage does not connect an Agent runtime, execute commands, or display evaluation results.
