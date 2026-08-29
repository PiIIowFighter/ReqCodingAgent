from __future__ import annotations

from pathlib import Path

from .errors import EvalError


def require_frozen_baseline(project_root: Path, name: str) -> Path:
    root = project_root / "configs/frozen" / name
    required = ("baseline.json", "checksums.sha256")
    missing = [item for item in required if not (root / item).is_file()]
    if missing:
        raise EvalError(f"Frozen baseline is unavailable: {name}; missing {missing}", category="invalid")
    return root


def refuse_unimplemented(operation: str) -> None:
    raise EvalError(f"{operation} is closed at the Iteration 2 implementation checkpoint", category="invalid")
