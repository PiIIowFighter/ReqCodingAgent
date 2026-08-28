from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq

from .config import Settings
from .errors import EvalError
from .frozen_cases import build_oracles, case_definitions, transform_prompt
from .locks import verify_source_locks
from .schema import validate_json, validate_jsonl


@dataclass(frozen=True)
class PreparedData:
    public_manifest: Path
    oracle_manifest: Path
    source_rows: dict[str, dict]
    lite_ids: set[str]
    lock_heads: dict[str, str]


def _parquet_rows(root: Path) -> list[dict]:
    files = sorted((root / "data").glob("*.parquet"))
    if not files:
        raise EvalError(f"No parquet data found under {root / 'data'}", hint="Use a complete checkout of the locked dataset revision")
    rows = []
    for file in files:
        rows.extend(pq.read_table(file).to_pylist())
    return rows


def _tests(value: object, field: str, instance_id: str) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise EvalError(f"{field} for {instance_id} is not a JSON array") from exc
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise EvalError(f"{field} for {instance_id} must be a string array")
    return value


def _atomic_jsonl(path: Path, records: list[dict], schema: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    for record in records:
        validate_json(record, schema)
    fd, temporary = tempfile.mkstemp(prefix=path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def prepare_data(settings: Settings, *, lock_path: Path | None = None, allow_fixture_hashes: bool = False) -> PreparedData:
    heads = verify_source_locks(settings, lock_path)
    definitions = case_definitions()
    verified = {row["instance_id"]: row for row in _parquet_rows(settings.cache_root / "verified") if row.get("instance_id")}
    lite_ids = {row["instance_id"] for row in _parquet_rows(settings.cache_root / "lite") if row.get("instance_id")}
    required = {case["instance_id"] for case in definitions}
    missing_verified = required - verified.keys()
    missing_lite = required - lite_ids
    if missing_verified:
        raise EvalError(f"Verified dataset is missing frozen IDs: {', '.join(sorted(missing_verified))}")
    if missing_lite:
        raise EvalError(f"Lite membership check failed; missing frozen IDs: {', '.join(sorted(missing_lite))}", hint="Use the exact locked Lite checkout")
    public = []
    selected = {}
    for definition in definitions:
        instance_id = definition["instance_id"]
        row = verified[instance_id]
        for field in ("repo", "base_commit"):
            if row.get(field) != definition[field]:
                raise EvalError(f"Verified {field} mismatch for {instance_id}: expected {definition[field]}, got {row.get(field)}")
        prompt = row.get("problem_statement")
        if not isinstance(prompt, str):
            raise EvalError(f"Verified problem_statement missing for {instance_id}")
        actual_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        if not allow_fixture_hashes and actual_hash != definition["original_prompt_sha256"]:
            raise EvalError(f"Verified prompt hash mismatch for {instance_id}: expected {definition['original_prompt_sha256']}, got {actual_hash}", hint="Use the exact locked Verified parquet without normalization")
        selected[instance_id] = row
        tests = {"FAIL_TO_PASS": _tests(row.get("FAIL_TO_PASS"), "FAIL_TO_PASS", instance_id), "PASS_TO_PASS": _tests(row.get("PASS_TO_PASS"), "PASS_TO_PASS", instance_id)}
        fuzzy = transform_prompt(instance_id, prompt)
        common = {"schema_version": "1.0", "split": definition["split"], "pair_id": definition["pair_id"], "instance_id": instance_id, "repo": definition["repo"], "base_commit": definition["base_commit"], "source_dataset": "SWE-bench Verified", "source_revision": heads["verified"], "original_prompt_sha256": actual_hash if allow_fixture_hashes else definition["original_prompt_sha256"], "approval_status": "frozen"} | tests
        public.append(common | {"case_id": definition["case_id"] + "-full", "prompt_variant": "full", "ambiguity_type": None, "severity": None, "prompt_sha256": actual_hash, "prompt": prompt, "transformation_description": None, "hidden_fact_id": None})
        public.append(common | {"case_id": definition["case_id"] + "-fuzzy", "prompt_variant": "fuzzy", "ambiguity_type": definition["ambiguity_type"], "severity": "medium", "prompt_sha256": hashlib.sha256(fuzzy.encode("utf-8")).hexdigest(), "prompt": fuzzy, "transformation_description": definition["transformation_description"], "hidden_fact_id": definition["hidden_fact_id"]})
    public_path = settings.project_root / "benchmark" / "manifests" / "paired-cases.jsonl"
    oracle_path = settings.project_root / "benchmark" / "private" / "oracles.jsonl"
    _atomic_jsonl(public_path, public, "public-case")
    _atomic_jsonl(oracle_path, build_oracles(), "oracle")
    return PreparedData(public_path, oracle_path, selected, lite_ids, heads)


def load_prepared(settings: Settings, *, lock_path: Path | None = None) -> PreparedData:
    heads = verify_source_locks(settings, lock_path)
    public_path = settings.project_root / "benchmark" / "manifests" / "paired-cases.jsonl"
    oracle_path = settings.project_root / "benchmark" / "private" / "oracles.jsonl"
    records = validate_jsonl(public_path, "public-case")
    required = {record["instance_id"] for record in records}
    verified = {row["instance_id"]: row for row in _parquet_rows(settings.cache_root / "verified") if row.get("instance_id") in required}
    lite_ids = {row["instance_id"] for row in _parquet_rows(settings.cache_root / "lite") if row.get("instance_id")}
    return PreparedData(public_path, oracle_path, verified, lite_ids, heads)
