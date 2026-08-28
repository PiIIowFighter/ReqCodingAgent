from __future__ import annotations

import json
from pathlib import Path

import pytest

from evalsys.config import Settings
from evalsys.errors import EvalError
from evalsys.recovery import artifact_hashes_valid, fingerprint
from evalsys.validation import validate_pairs
from evalsys.verdict import decide_verdict


def test_cache_must_be_outside_project(tmp_path: Path):
    with pytest.raises(EvalError):
        Settings(project_root=tmp_path, cache_root=tmp_path / "cache", artifact_root=tmp_path / "artifacts")


def test_noop_and_gold_are_decided_per_test():
    f2p, p2p = ["fails"], ["stable"]
    assert decide_verdict("noop", {"fails": "FAILED", "stable": "PASSED"}, f2p, p2p)["status"] == "passed"
    assert decide_verdict("noop", {"fails": "PASSED", "stable": "PASSED"}, f2p, p2p)["status"] == "test_failed"
    assert decide_verdict("gold", {"fails": "PASSED", "stable": "PASSED"}, f2p, p2p)["status"] == "passed"
    assert decide_verdict("gold", {"stable": "PASSED"}, f2p, p2p)["status"] == "invalid"


def test_recovery_requires_all_artifact_hashes(tmp_path: Path):
    output = tmp_path / "result.json"
    output.write_text("{}", encoding="utf-8")
    import hashlib
    expected = {"result.json": hashlib.sha256(output.read_bytes()).hexdigest()}
    assert artifact_hashes_valid(tmp_path, expected)
    output.write_text("changed", encoding="utf-8")
    assert not artifact_hashes_valid(tmp_path, expected)
    assert fingerprint({"b": 2, "a": 1}) == fingerprint({"a": 1, "b": 2})


def test_pair_validator_rejects_distribution_mismatch():
    with pytest.raises(EvalError, match="pairs"):
        validate_pairs([])
