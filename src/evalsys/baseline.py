from __future__ import annotations

import random
from pathlib import Path
from typing import Any

from .errors import EvalError


FORMAL_SEED = 20260828
FORMAL_INSTANCES = (
    "astropy__astropy-14995",
    "pydata__xarray-4094",
    "pytest-dev__pytest-7432",
    "matplotlib__matplotlib-25311",
    "django__django-10914",
    "matplotlib__matplotlib-23476",
    "scikit-learn__scikit-learn-13439",
    "sphinx-doc__sphinx-8595",
    "django__django-13933",
    "psf__requests-2317",
    "scikit-learn__scikit-learn-13779",
    "sphinx-doc__sphinx-8721",
)


def build_formal_plan(records: list[dict[str, Any]], *, seed: int = FORMAL_SEED) -> list[dict[str, Any]]:
    """Build the frozen paired plan without exposing private case fields."""
    by_instance: dict[str, dict[str, dict[str, Any]]] = {}
    for record in records:
        if record.get("split") != "test" or record.get("prompt_variant") not in {"full", "fuzzy"}:
            continue
        by_instance.setdefault(record["instance_id"], {})[record["prompt_variant"]] = record
    if len(by_instance) != 12 or any(set(pair) != {"full", "fuzzy"} for pair in by_instance.values()):
        raise EvalError("formal plan requires exactly 12 complete test pairs", category="invalid")
    if set(by_instance) != set(FORMAL_INSTANCES):
        raise EvalError("formal plan instances do not match the canonical task list", category="invalid")
    instance_ids = list(FORMAL_INSTANCES)
    rng = random.Random(seed)
    rng.shuffle(instance_ids)
    full_first = set(rng.sample(instance_ids, 6))
    plan: list[dict[str, Any]] = []
    for instance_id in instance_ids:
        variants = ("full", "fuzzy") if instance_id in full_first else ("fuzzy", "full")
        for variant in variants:
            record = by_instance[instance_id][variant]
            plan.append({
                "sequence": len(plan) + 1,
                "case_id": record["case_id"],
                "instance_id": instance_id,
                "variant": variant,
                "experiment": "E1" if variant == "full" else "E2",
                "ambiguity_type": record["ambiguity_type"],
                "prompt_sha256": record["prompt_sha256"],
                "base_commit": record["base_commit"],
            })
    return plan


def verify_formal_plan(plan: list[dict[str, Any]], records: list[dict[str, Any]], *, seed: int = FORMAL_SEED) -> None:
    if plan != build_formal_plan(records, seed=seed):
        raise EvalError("formal plan does not match the deterministic frozen plan", category="invalid")


def require_frozen_baseline(project_root: Path, name: str) -> Path:
    root = project_root / "configs/frozen" / name
    required = ("baseline.json", "checksums.sha256")
    missing = [item for item in required if not (root / item).is_file()]
    if missing:
        raise EvalError(f"Frozen baseline is unavailable: {name}; missing {missing}", category="invalid")
    return root


def refuse_unimplemented(operation: str) -> None:
    raise EvalError(f"{operation} is closed at the Iteration 2 implementation checkpoint", category="invalid")
