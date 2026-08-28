from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from .errors import EvalError


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_schema(name: str) -> dict:
    path = _root() / "benchmark" / "schemas" / f"{name}.schema.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvalError(f"Unable to load schema {name}: {exc}", hint=f"Check {path}") from exc
    Draft202012Validator.check_schema(value)
    return value


def validate_json(value: object, schema_name: str) -> None:
    errors = sorted(Draft202012Validator(load_schema(schema_name)).iter_errors(value), key=lambda error: list(error.path))
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "$"
        raise EvalError(f"{schema_name} schema validation failed at {location}: {error.message}", hint="Remove unknown fields and supply every required field")


def validate_jsonl(path: Path, schema_name: str) -> list[dict]:
    records = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise EvalError(f"Cannot read {path}: {exc}") from exc
    for number, line in enumerate(lines, 1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvalError(f"Malformed JSONL at {path}, line {number}: {exc.msg}") from exc
        try:
            validate_json(record, schema_name)
        except EvalError as exc:
            raise EvalError(f"{path} line {number}: {exc.message}", hint=exc.hint) from exc
        records.append(record)
    return records
