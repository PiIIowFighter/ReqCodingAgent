from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .trace import atomic_json, atomic_text


SCHEMA_VERSION = 1


def canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class CheckpointStore:
    def __init__(self, run_path: Path):
        self.run_path = run_path
        self.root = run_path / "checkpoints"
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, sequence: int, payload: dict[str, Any]) -> Path:
        body = {"schema_version": SCHEMA_VERSION, "payload": payload}
        body["checksum"] = canonical_hash(body)
        target = self.root / f"{sequence:06d}.json"
        atomic_json(target, body)
        loaded = json.loads(target.read_text(encoding="utf-8"))
        self._verify(loaded)
        atomic_text(self.run_path / "LATEST", target.name + "\n")
        return target

    def load(self) -> dict[str, Any]:
        if (self.run_path / "COMPLETE").exists():
            raise ValueError("completed runs cannot be resumed")
        latest = (self.run_path / "LATEST").read_text(encoding="utf-8").strip()
        value = json.loads((self.root / latest).read_text(encoding="utf-8"))
        self._verify(value)
        return value["payload"]

    @staticmethod
    def _verify(value: dict[str, Any]) -> None:
        if set(value) != {"schema_version", "payload", "checksum"} or value["schema_version"] != SCHEMA_VERSION:
            raise ValueError("invalid checkpoint schema")
        expected = value["checksum"]
        body = {"schema_version": value["schema_version"], "payload": value["payload"]}
        if canonical_hash(body) != expected:
            raise ValueError("checkpoint checksum mismatch")
