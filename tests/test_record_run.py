from __future__ import annotations

import importlib.util
import io
from pathlib import Path


def _module():
    path = Path(__file__).resolve().parents[1] / "scripts/record_run.py"
    spec = importlib.util.spec_from_file_location("record_run", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def test_decode_output_handles_non_gbk_bytes():
    module = _module()
    assert module.decode_output(b"passed: \xff\n") == "passed: �\n"


def test_emit_output_writes_original_bytes_without_console_encoding():
    module = _module()
    target = io.BytesIO()
    module.emit_output(target, b"passed: \xff\n")
    assert target.getvalue() == b"passed: \xff\n"
