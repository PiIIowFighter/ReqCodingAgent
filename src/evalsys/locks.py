from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .config import Settings
from .errors import EvalError
from .schema import validate_json


def _git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(["git", "-C", str(repo), *args], check=True, text=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise EvalError(f"Cannot inspect Git source at {repo}: {detail.strip()}", hint="Populate the configurable external cache with the locked checkout") from exc
    return result.stdout.strip()


def verify_source_locks(settings: Settings, lock_path: Path | None = None) -> dict[str, str]:
    path = lock_path or settings.project_root / "benchmark" / "source-lock.json"
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvalError(f"Cannot read source lock {path}: {exc}") from exc
    validate_json(lock, "source-lock")
    result = {}
    for name, source in lock["sources"].items():
        checkout = settings.cache_root / ({"harness": "swe-bench"}.get(name, name))
        if not (checkout / ".git").exists():
            raise EvalError(f"Locked {name} checkout is missing: {checkout}", hint=f"Clone {source['url']} outside the project and checkout {source['revision']}")
        head = _git(checkout, "rev-parse", "HEAD")
        if head != source["revision"]:
            raise EvalError(f"Locked {name} HEAD mismatch: expected {source['revision']}, got {head}", hint="Checkout the exact locked commit; branches are not accepted")
        if _git(checkout, "status", "--porcelain"):
            raise EvalError(f"Locked {name} checkout is dirty: {checkout}", hint="Use a clean checkout so data provenance is auditable")
        result[name] = head
    return result
