from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .recovery import fingerprint, sha256_file

_REQUIRED_AUDIT = ("summary.json", "command.txt", "config-summary.json", "result-summary.json", "log-index.json")
_SECRET = re.compile(r"(?i)(api[_-]?key|token|password|secret)\s*[=:]\s*\S+")
_SSH = re.compile(r"(?i)(ssh-(?:rsa|ed25519))\s+\S+")
_WINDOWS_ABS = re.compile(r"[A-Za-z]:\\[^\r\n\t\"]+")
_WSL_ABS = re.compile(r"/mnt/[a-z]/[^\r\n\t\"]+")


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def sanitize(value: Any, *, project_root: Path) -> Any:
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if re.search(r"(?i)key|token|password|secret", key) else sanitize(item, project_root=project_root)) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize(item, project_root=project_root) for item in value]
    if not isinstance(value, str):
        return value
    result = value.replace(str(project_root), "<PROJECT_ROOT>").replace(project_root.as_posix(), "<PROJECT_ROOT>")
    result = _SECRET.sub(lambda match: match.group(1) + "=[REDACTED]", result)
    result = _SSH.sub("[REDACTED_SSH]", result)
    result = _WINDOWS_ABS.sub("<ABSOLUTE_PATH>", result)
    result = _WSL_ABS.sub("<ABSOLUTE_PATH>", result)
    return result[:8192]


def _checksums(directory: Path, names: list[str]) -> None:
    lines = [f"{sha256_file(directory / name)}  {name}" for name in sorted(names)]
    (directory / "checksums.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")


@dataclass
class EvidenceRun:
    recorder: "EvidenceRecorder"
    run_id: str
    run_type: str
    config_hash: str
    raw_dir: Path
    audit_dir: Path
    command: list[str]
    config: dict[str, Any]

    def record_event(self, event: str, details: dict[str, Any]) -> None:
        with (self.raw_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": event, "details": details}, ensure_ascii=False, sort_keys=True) + "\n")

    def finish(self, result: dict[str, Any], *, stdout: str = "", stderr: str = "") -> None:
        self._close(result, stdout, stderr, "COMPLETE")

    def fail(self, result: dict[str, Any], *, stdout: str = "", stderr: str = "") -> None:
        self._close(result, stdout, stderr, "FAILED")

    def _close(self, result: dict[str, Any], stdout: str, stderr: str, marker: str) -> None:
        (self.raw_dir / "stdout.log").write_text(stdout, encoding="utf-8")
        (self.raw_dir / "stderr.log").write_text(stderr, encoding="utf-8")
        _atomic_json(self.raw_dir / "result.json", result)
        raw_names = ["run-manifest.json", "config.snapshot.json", "events.jsonl", "stdout.log", "stderr.log", "result.json"]
        _checksums(self.raw_dir, raw_names)
        (self.raw_dir / marker).write_text("\n", encoding="utf-8")
        sanitized_config = sanitize(self.config, project_root=self.recorder.project_root)
        sanitized_result = sanitize(result, project_root=self.recorder.project_root)
        _atomic_json(self.audit_dir / "summary.json", {"run_id": self.run_id, "run_type": self.run_type, "status": result.get("status", "unknown"), "config_hash": self.config_hash})
        (self.audit_dir / "command.txt").write_text(" ".join(sanitize(self.command, project_root=self.recorder.project_root)) + "\n", encoding="utf-8")
        _atomic_json(self.audit_dir / "config-summary.json", sanitized_config)
        _atomic_json(self.audit_dir / "result-summary.json", sanitized_result)
        _atomic_json(self.audit_dir / "log-index.json", {"stdout": "retained in local artifacts", "stderr": "retained in local artifacts", "raw_checksums": "checksums.sha256"})
        _checksums(self.audit_dir, list(_REQUIRED_AUDIT))
        self.recorder._append_index(self, str(result.get("status", "unknown")))


class EvidenceRecorder:
    def __init__(self, project_root: Path, *, iteration: int) -> None:
        self.project_root = project_root.resolve()
        self.iteration = iteration
        self.raw_root = self.project_root / f"artifacts/runs/iteration{iteration}"
        self.audit_root = self.project_root / f"audit/iteration{iteration}"

    def start(self, run_type: str, config: dict[str, Any], command: list[str], *, now: str | None = None) -> EvidenceRun:
        timestamp = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        compact = timestamp.replace("-", "").replace(":", "").replace("T", "T").removesuffix("Z") + "Z"
        config_hash = fingerprint(config)[:10]
        run_id = f"{compact}_{run_type}_{config_hash}"
        raw_dir = self.raw_root / run_id
        audit_dir = self.audit_root / "runs" / run_id
        raw_dir.mkdir(parents=True, exist_ok=False)
        try:
            audit_dir.mkdir(parents=True, exist_ok=False)
        except Exception:
            raw_dir.rmdir()
            raise
        manifest = {"run_id": run_id, "run_type": run_type, "iteration": self.iteration, "config_hash": config_hash, "command": command, "started_at": timestamp}
        _atomic_json(raw_dir / "run-manifest.json", manifest)
        _atomic_json(raw_dir / "config.snapshot.json", config)
        (raw_dir / "events.jsonl").write_text("", encoding="utf-8")
        return EvidenceRun(self, run_id, run_type, config_hash, raw_dir, audit_dir, command, config)

    def _append_index(self, run: EvidenceRun, status: str) -> None:
        index_path = self.audit_root / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {"schema_version": "1.0", "iteration": self.iteration, "runs": []}
        if any(entry["run_id"] == run.run_id for entry in index["runs"]):
            raise RuntimeError(f"Run already indexed: {run.run_id}")
        index["runs"].append({"run_id": run.run_id, "run_type": run.run_type, "status": status, "config_hash": run.config_hash, "raw_path": run.raw_dir.relative_to(self.project_root).as_posix(), "audit_path": run.audit_dir.relative_to(self.project_root).as_posix(), "validity": "active"})
        _atomic_json(index_path, index)
