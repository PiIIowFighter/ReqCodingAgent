from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .persistence import atomic_json as _atomic_json, file_lock, utf8_lf as _lf_bytes, write_text_lf as _write_text_lf
from .recovery import fingerprint, sha256_file

_REQUIRED_AUDIT = ("summary.json", "command.txt", "config-summary.json", "result-summary.json", "log-index.json")
_RESULT_FIELDS = (
    "status",
    "passed",
    "failed",
    "skipped",
    "duration",
    "exit_code",
    "classification",
    "reason",
)
_SECRET = re.compile(r"(?i)(api[_-]?key|token|password|secret)\s*[=:]\s*\S+")
_SSH = re.compile(r"(?i)(ssh-(?:rsa|ed25519))\s+\S+")
_WINDOWS_ABS = re.compile(r"[A-Za-z]:\\[^\r\n\t\"]+")
_WSL_ABS = re.compile(r"/mnt/[a-z]/[^\r\n\t\"]+")



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
    _write_text_lf(directory / "checksums.sha256", "\n".join(lines) + "\n")


def verify_checksums(directory: Path) -> list[str]:
    root = directory.resolve()
    mismatches: list[str] = []
    seen: set[str] = set()
    for line in (root / "checksums.sha256").read_bytes().decode("utf-8").splitlines():
        digest, name = line.split("  ", 1)
        candidate = Path(name)
        if candidate.is_absolute() or ".." in candidate.parts or "\\" in name or name in seen:
            raise ValueError(f"unsafe or duplicate checksum path: {name}")
        seen.add(name)
        path = root / candidate
        resolved = path.resolve()
        if root not in resolved.parents or any(part.is_symlink() for part in [path, *path.parents] if part != root):
            raise ValueError(f"checksum path escapes or uses symlink: {name}")
        if not path.is_file() or sha256_file(path) != digest:
            mismatches.append(name)
    return mismatches


def select_current_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate the supersession DAG and return active leaves per run type."""
    by_id: dict[str, dict[str, Any]] = {}
    for run in runs:
        run_id = run.get("run_id")
        if not isinstance(run_id, str) or run_id in by_id:
            raise ValueError(f"duplicate or invalid run_id: {run_id}")
        by_id[run_id] = run
    for run in runs:
        for prior in run.get("supersedes", []):
            if prior not in by_id:
                raise ValueError(f"unknown superseded run: {prior}")
            if by_id[prior].get("run_type") != run.get("run_type"):
                raise ValueError("supersession run_type mismatch")
    visiting: set[str] = set()
    visited: set[str] = set()
    def visit(run_id: str) -> None:
        if run_id in visiting:
            raise ValueError("supersession cycle detected")
        if run_id in visited:
            return
        visiting.add(run_id)
        for prior in by_id[run_id].get("supersedes", []):
            visit(prior)
        visiting.remove(run_id)
        visited.add(run_id)
    for run_id in by_id:
        visit(run_id)
    superseded = {prior for run in runs if run.get("validity") == "active" for prior in run.get("supersedes", [])}
    return [run for run in runs if run.get("validity") == "active" and run["run_id"] not in superseded]


def verify_active_audit_runs(project_root: Path, *, iteration: int) -> dict[str, list[str]]:
    root = project_root.resolve()
    index_path = root / f"audit/iteration{iteration}/index.json"
    index = json.loads(index_path.read_bytes().decode("utf-8"))
    result = {}
    for entry in index["runs"]:
        if entry.get("validity") != "active":
            continue
        run_id = entry["run_id"]
        expected = Path(f"audit/iteration{iteration}/runs") / run_id
        supplied = Path(entry["audit_path"])
        if supplied != expected or supplied.is_absolute() or ".." in supplied.parts:
            raise ValueError(f"unsafe audit path for {run_id}")
        resolved = (root / supplied).resolve()
        if resolved != (root / expected).resolve() or any(part.is_symlink() for part in [root / supplied, *(root / supplied).parents] if part != root):
            raise ValueError(f"audit path escapes through symlink for {run_id}")
        result[run_id] = verify_checksums(resolved)
    return result


def _file_metadata(path: Path) -> dict[str, Any]:
    return {"sha256": sha256_file(path), "bytes": path.stat().st_size}


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
    supersedes: list[str]
    indexed: bool = False
    attempt_dir: Path | None = None

    def _target_dir(self) -> Path:
        return self.attempt_dir or self.raw_dir

    def record_event(self, event: str, details: dict[str, Any]) -> None:
        target = self._target_dir()
        target.mkdir(parents=True, exist_ok=True)
        lock = target / ".events.lock"
        with file_lock(lock):
            with (target / "events.jsonl").open("ab") as stream:
                stream.write(_lf_bytes(json.dumps({"event": event, "details": details}, ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n"))
                stream.flush()
                import os
                os.fsync(stream.fileno())

    def finish(self, result: dict[str, Any], *, stdout: str = "", stderr: str = "") -> None:
        self._close(result, stdout, stderr, "COMPLETE")

    def fail(self, result: dict[str, Any], *, stdout: str = "", stderr: str = "") -> None:
        self._close(result, stdout, stderr, "FAILED")

    def _close(self, result: dict[str, Any], stdout: str, stderr: str, marker: str) -> None:
        if self.indexed and self.attempt_dir is None:
            raise RuntimeError(f"Evidence run already finalized: {self.run_id}")
        target = self._target_dir()
        if self.attempt_dir is not None:
            json.dumps(result, ensure_ascii=False, allow_nan=False)
            if (target / "COMPLETE").exists() or (target / "FAILED").exists():
                raise RuntimeError(f"Resume attempt already finalized: {target.name}")
            _write_text_lf(target / "stdout.log", stdout)
            _write_text_lf(target / "stderr.log", stderr)
            _atomic_json(target / "result.json", result)
            names = ["events.jsonl", "stdout.log", "stderr.log", "result.json"]
            _checksums(target, names)
            _atomic_json(target / marker, {"schema_version": "1.0", "run_id": self.run_id, "attempt_id": target.name, "status": result.get("status", "unknown"), "indexed": False})
            return
        # Validate serialization before writing any terminal artifact.
        json.dumps(result, ensure_ascii=False, allow_nan=False)
        if (self.raw_dir / "COMPLETE").exists() or (self.raw_dir / "FAILED").exists():
            raise RuntimeError(f"Evidence run already has terminal marker: {self.run_id}")
        _write_text_lf(self.raw_dir / "stdout.log", stdout)
        _write_text_lf(self.raw_dir / "stderr.log", stderr)
        _atomic_json(self.raw_dir / "result.json", result)
        raw_names = ["run-manifest.json", "config.snapshot.json", "events.jsonl", "stdout.log", "stderr.log", "result.json"]
        _checksums(self.raw_dir, raw_names)
        sanitized_config = sanitize(self.config, project_root=self.recorder.project_root)
        result_summary = {
            field: sanitize(
                result.get(field, 0 if field in {"passed", "failed", "skipped"} else None),
                project_root=self.recorder.project_root,
            )
            for field in _RESULT_FIELDS
            if field not in {"classification", "reason"}
            or result.get("status") != "passed"
            or field in result
        }
        _atomic_json(self.audit_dir / "summary.json", {"run_id": self.run_id, "run_type": self.run_type, "status": result.get("status", "unknown"), "config_hash": self.config_hash, "supersedes": self.supersedes})
        _write_text_lf(self.audit_dir / "command.txt", " ".join(sanitize(self.command, project_root=self.recorder.project_root)) + "\n")
        _atomic_json(self.audit_dir / "config-summary.json", sanitized_config)
        _atomic_json(self.audit_dir / "result-summary.json", result_summary)
        raw_index = {name: _file_metadata(self.raw_dir / name) for name in ("stdout.log", "stderr.log", "result.json", "checksums.sha256")}
        _atomic_json(self.audit_dir / "log-index.json", raw_index)
        _checksums(self.audit_dir, list(_REQUIRED_AUDIT))
        if not self.indexed:
            self.recorder._append_index(self, str(result.get("status", "unknown")))
            self.indexed = True
        terminal = {"schema_version": "1.0", "run_id": self.run_id, "status": result.get("status", "unknown"), "result": "result.json", "checksums": "checksums.sha256", "audit_path": f"audit/iteration{self.recorder.iteration}/runs/{self.run_id}", "indexed": True}
        _atomic_json(self.raw_dir / marker, terminal)


class EvidenceRecorder:
    def __init__(self, project_root: Path, *, iteration: int, raw_root: Path | None = None) -> None:
        self.project_root = project_root.resolve()
        self.iteration = iteration
        self.raw_root = raw_root.resolve() if raw_root is not None else self.project_root / f"artifacts/runs/iteration{iteration}"
        self.audit_root = self.project_root / f"audit/iteration{iteration}"

    def start(self, run_type: str, config: dict[str, Any], command: list[str], *, now: str | None = None, supersedes: list[str] | None = None) -> EvidenceRun:
        timestamp = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        compact = timestamp.replace("-", "").replace(":", "").removesuffix("Z") + "Z"
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
        relationship = list(supersedes or [])
        manifest = {"run_id": run_id, "run_type": run_type, "iteration": self.iteration, "config_hash": config_hash, "command": command, "started_at": timestamp, "supersedes": relationship}
        _atomic_json(raw_dir / "run-manifest.json", manifest)
        _atomic_json(raw_dir / "config.snapshot.json", config)
        _write_text_lf(raw_dir / "events.jsonl", "")
        return EvidenceRun(self, run_id, run_type, config_hash, raw_dir, audit_dir, command, config, relationship)

    def start_explicit(self, run_id: str, run_type: str, config: dict[str, Any], command: list[str], *, existing_raw_dir: Path, resume: bool = False, supersedes: list[str] | None = None) -> EvidenceRun:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", run_id):
            raise ValueError("unsafe explicit run_id")
        raw_dir = existing_raw_dir.resolve()
        expected = (self.raw_root / run_id).resolve()
        if raw_dir != expected or not raw_dir.is_dir():
            raise ValueError("explicit raw directory must be the matching recorder run root")
        audit_dir = self.audit_root / "runs" / run_id
        index_path = self.audit_root / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {"runs": []}
        indexed = any(entry["run_id"] == run_id for entry in index.get("runs", []))
        if resume:
            if not indexed or not audit_dir.is_dir():
                raise ValueError("resume requires existing audit and index identity")
            attempts = raw_dir / "attempts"
            attempts.mkdir(exist_ok=True)
            attempt_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "_" + fingerprint({"config": config, "command": command})[:10]
            attempt_dir = attempts / attempt_id
            attempt_dir.mkdir(exist_ok=False)
            _write_text_lf(attempt_dir / "events.jsonl", "")
            return EvidenceRun(self, run_id, run_type, fingerprint(config)[:10], raw_dir, audit_dir, command, config, list(supersedes or []), indexed=True, attempt_dir=attempt_dir)
        if indexed or audit_dir.exists() or (raw_dir / "run-manifest.json").exists():
            raise FileExistsError(run_id)
        audit_dir.mkdir(parents=True, exist_ok=False)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        config_hash = fingerprint(config)[:10]
        relationship = list(supersedes or [])
        manifest = {"run_id": run_id, "run_type": run_type, "iteration": self.iteration, "config_hash": config_hash, "command": command, "started_at": timestamp, "supersedes": relationship}
        _atomic_json(raw_dir / "run-manifest.json", manifest)
        _atomic_json(raw_dir / "config.snapshot.json", config)
        _write_text_lf(raw_dir / "events.jsonl", "")
        return EvidenceRun(self, run_id, run_type, config_hash, raw_dir, audit_dir, command, config, relationship)

    def invalidate_runs(self, run_ids: list[str], *, reason: str) -> None:
        index_path = self.audit_root / "index.json"
        index = json.loads(index_path.read_bytes().decode("utf-8"))
        found = set()
        for entry in index["runs"]:
            if entry["run_id"] in run_ids:
                entry["validity"] = "invalid"
                entry["invalid_reason"] = reason
                found.add(entry["run_id"])
        missing = set(run_ids) - found
        if missing:
            raise KeyError(f"Unknown run ids: {sorted(missing)}")
        _atomic_json(index_path, index)

    def _append_index(self, run: EvidenceRun, status: str) -> None:
        index_path = self.audit_root / "index.json"
        index = json.loads(index_path.read_bytes().decode("utf-8")) if index_path.exists() else {"schema_version": "1.0", "iteration": self.iteration, "runs": []}
        index["validity_semantics"] = "active means evidence checksums are valid; current results are active leaves of the supersedes graph"
        if any(entry["run_id"] == run.run_id for entry in index["runs"]):
            raise RuntimeError(f"Run already indexed: {run.run_id}")
        index["runs"].append({"run_id": run.run_id, "run_type": run.run_type, "status": status, "config_hash": run.config_hash, "raw_path": run.raw_dir.relative_to(self.project_root).as_posix(), "audit_path": run.audit_dir.relative_to(self.project_root).as_posix(), "validity": "active", "supersedes": run.supersedes})
        _atomic_json(index_path, index)
