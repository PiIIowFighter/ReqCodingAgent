# Local Requirement Explorer

A dependency-free, read-only desktop-style viewer for the frozen requirement ontology.

## Run

Python 3.11 or newer is required.

```sh
python -m demo_gui.server
```

Open `http://127.0.0.1:8765`. Use `--port` to select another port.

The server verifies the ontology SHA-256 against `configs/frozen/baseline-v3/baseline.json` before listening. A mismatch stops startup. It serves only an explicit static allowlist and three GET-only JSON endpoints; it does not execute requests or write project data.

## Check

```sh
python -m unittest tests.test_demo_gui -v
python -m py_compile demo_gui/server.py
```
