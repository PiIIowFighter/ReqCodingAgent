from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .errors import EvalError


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def strict_json_loads(payload: str) -> Any:
    return json.loads(payload, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_nonfinite)


def load_json(path: Path) -> Any:
    try:
        return strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise EvalError(f"Unable to load JSON {path}: {exc}", hint="Use strict RFC-compatible JSON with unique object keys") from exc


def load_schema(name: str) -> dict:
    if not re.fullmatch(r"[a-z0-9-]+", name):
        raise EvalError(f"Invalid schema name: {name!r}", hint="Use lowercase letters, digits, and hyphens only")
    path = _root() / "benchmark" / "schemas" / f"{name}.schema.json"
    try:
        value = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise EvalError(f"Unable to load schema {name}: {exc}", hint=f"Check {path}") from exc
    Draft202012Validator.check_schema(value)
    return value


@lru_cache(maxsize=None)
def _validator(schema_name: str) -> Draft202012Validator:
    return Draft202012Validator(load_schema(schema_name), format_checker=FormatChecker())


def validate_json(value: object, schema_name: str) -> None:
    errors = sorted(_validator(schema_name).iter_errors(value), key=lambda error: list(error.path))
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
            record = strict_json_loads(line)
        except (json.JSONDecodeError, ValueError) as exc:
            message = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
            raise EvalError(f"Malformed JSONL at {path}, line {number}: {message}") from exc
        try:
            validate_json(record, schema_name)
        except EvalError as exc:
            raise EvalError(f"{path} line {number}: {exc.message}", hint=exc.hint) from exc
        records.append(record)
    return records
