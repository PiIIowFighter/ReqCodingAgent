from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def utf8_lf(text: str) -> bytes:
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def write_text_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(utf8_lf(text))


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(utf8_lf(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
