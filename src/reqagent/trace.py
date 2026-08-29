from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path: Path, value: Any) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n")


class RunStore:
    def __init__(self, root: Path, run_id: str, *, create: bool = True):
        self.root = root.resolve()
        self.run_id = run_id
        self.path = self.root / run_id
        if create:
            self.path.mkdir(parents=True, exist_ok=False)
            (self.path / "checkpoints").mkdir()

    @classmethod
    def create(cls, root: Path, kind: str = "offline") -> "RunStore":
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        return cls(root, f"{stamp}-{kind}-{os.getpid():x}")

    @classmethod
    def open(cls, path: Path) -> "RunStore":
        path = path.resolve()
        if not path.is_dir():
            raise ValueError("run directory does not exist")
        return cls(path.parent, path.name, create=False)

    def event(self, kind: str, **data: Any) -> None:
        record = {"kind": kind, **data}
        with (self.path / "events.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
