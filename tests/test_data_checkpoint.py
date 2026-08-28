from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from evalsys.config import Settings
from evalsys.data import prepare_data
from evalsys.errors import EvalError
from evalsys.frozen_cases import CASE_IDS, build_oracles, case_definitions
from evalsys.locks import verify_source_locks
from evalsys.schema import validate_json, validate_jsonl
from evalsys.validation import validate_benchmark


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(path), *args], check=True, text=True, capture_output=True)
    return result.stdout.strip()


def _repo(path: Path) -> str:
    path.mkdir(parents=True)
    _git(path, "init")
    _git(path, "config", "user.email", "fixture@example.invalid")
    _git(path, "config", "user.name", "Fixture")
    (path / "data").mkdir()
    (path / "data" / ".keep").write_text("fixture", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "fixture")
    return _git(path, "rev-parse", "HEAD")


def _settings(tmp_path: Path) -> Settings:
    return Settings(tmp_path / "project", tmp_path / "cache", tmp_path / "artifacts")


def test_strict_schema_rejects_unknown_fields(tmp_path: Path):
    valid = json.loads((Path(__file__).parents[1] / "benchmark/source-lock.json").read_text(encoding="utf-8"))
    validate_json(valid, "source-lock")
    with pytest.raises(EvalError, match="schema"):
        validate_json({**valid, "unexpected": True}, "source-lock")
    malformed = tmp_path / "bad.jsonl"
    malformed.write_text('{"not": "a public case"}\n', encoding="utf-8")
    with pytest.raises(EvalError, match="line 1"):
        validate_jsonl(malformed, "public-case")


def test_frozen_case_metadata_is_exact_and_balanced():
    definitions = case_definitions()
    assert len(CASE_IDS) == len(set(CASE_IDS)) == 15
    assert len(definitions) == 15
    assert sum(case["split"] == "test" for case in definitions) == 12
    assert sum(case["split"] == "dev" for case in definitions) == 3
    assert len(build_oracles()) == 15
    for case in definitions:
        assert len(case["original_prompt_sha256"]) == 64
        assert len(case["base_commit"]) == 40
        assert case["severity"] == "medium"
        assert case["approval_status"] == "frozen"
        assert case["transformation_description"]


def test_lock_verification_checks_real_heads(tmp_path: Path):
    settings = _settings(tmp_path)
    settings.project_root.mkdir()
    revisions = {}
    for name in ("harness", "verified", "lite"):
        revisions[name] = _repo(settings.cache_root / ("swe-bench" if name == "harness" else name))
    lock = {"schema_version": "1.0", "sources": {name: {"url": f"https://example.invalid/{name}", "revision": rev} for name, rev in revisions.items()}}
    lock_path = tmp_path / "lock.json"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    result = verify_source_locks(settings, lock_path)
    assert result == revisions
    (settings.cache_root / "lite" / "dirty").write_text("x", encoding="utf-8")
    with pytest.raises(EvalError, match="dirty"):
        verify_source_locks(settings, lock_path)


def test_prepare_and_validate_from_small_parquet_fixtures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = _settings(tmp_path)
    settings.project_root.mkdir()
    definitions = case_definitions()
    revisions = {}
    for name in ("harness", "verified", "lite"):
        revisions[name] = _repo(settings.cache_root / ("swe-bench" if name == "harness" else name))
    rows = []
    for case in definitions:
        # Fixture prompts intentionally use the frozen source text supplied by each definition.
        prompt = case["fixture_prompt"]
        rows.append({
            "instance_id": case["instance_id"], "repo": case["repo"],
            "base_commit": case["base_commit"], "problem_statement": prompt,
            "patch": "SECRET PATCH", "test_patch": "SECRET TEST PATCH", "hints_text": "SECRET HINT",
            "FAIL_TO_PASS": json.dumps(case["fixture_fail_to_pass"]),
            "PASS_TO_PASS": json.dumps(case["fixture_pass_to_pass"]),
        })
    pq.write_table(pa.Table.from_pylist(rows), settings.cache_root / "verified" / "data" / "test.parquet")
    pq.write_table(pa.Table.from_pylist([{"instance_id": case["instance_id"]} for case in definitions]), settings.cache_root / "lite" / "data" / "test.parquet")
    for name in ("verified", "lite"):
        _git(settings.cache_root / name, "add", ".")
        _git(settings.cache_root / name, "commit", "-m", "data fixture")
        revisions[name] = _git(settings.cache_root / name, "rev-parse", "HEAD")
    lock = {"schema_version": "1.0", "sources": {name: {"url": f"https://example.invalid/{name}", "revision": rev} for name, rev in revisions.items()}}
    lock_path = tmp_path / "lock.json"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    # Fixture hashes are derived from fixture_prompt instead of production frozen hashes.
    prepared = prepare_data(settings, lock_path=lock_path, allow_fixture_hashes=True)
    report = validate_benchmark(prepared, allow_fixture_hashes=True)
    assert report["test_pairs"] == 12
    assert report["dev_pairs"] == 3
    public_text = prepared.public_manifest.read_text(encoding="utf-8")
    assert "SECRET PATCH" not in public_text
    assert "SECRET TEST PATCH" not in public_text
    assert "SECRET HINT" not in public_text
    assert all(record["approval_status"] == "frozen" for record in validate_jsonl(prepared.public_manifest, "public-case"))


def test_transformations_hide_frozen_specific_details():
    from evalsys.frozen_cases import transform_prompt

    samples = {
        "matplotlib__matplotlib-23476": "[Bug]: DPI of a figure is doubled after unpickling on M1 Mac\n### Bug summary\n\nWhen a figure is unpickled, it's dpi is doubled.\n### Actual outcome\n200 400 800\n### Expected outcome\n200 200\n### Additional information\nM1",
        "sphinx-doc__sphinx-8595": "autodoc: empty __all__ attribute is ignored\n**Describe the bug**\nautodoc: empty `__all__` attribute is ignored\n__all__ = []\nAll foo, bar, and baz are shown.\n**Expected behavior**\nNo entries should be shown because `__all__` is empty.",
        "matplotlib__matplotlib-25332": "[Bug]: Unable to pickle figure with aligned labels\n### Bug summary\nUnable to pickle figure after calling `align_labels()`\n### Code\nfig.align_labels() ##pickling works after removing this line \npickle.dumps(fig)\n### Actual outcome\nTypeError",
    }
    dpi = transform_prompt("matplotlib__matplotlib-23476", samples["matplotlib__matplotlib-23476"])
    assert "dpi is doubled" not in dpi.lower()
    assert "200 400 800" not in dpi and "200 200" not in dpi
    sphinx = transform_prompt("sphinx-doc__sphinx-8595", samples["sphinx-doc__sphinx-8595"])
    assert "empty __all__" not in sphinx and "`__all__` is empty" not in sphinx
    aligned = transform_prompt("matplotlib__matplotlib-25332", samples["matplotlib__matplotlib-25332"])
    assert "after calling `align_labels()`" not in aligned
    assert "fig.align_labels()" in aligned and "pickle.dumps(fig)" in aligned


def test_prepare_rejects_missing_lite_member(tmp_path: Path):
    settings = _settings(tmp_path)
    settings.project_root.mkdir()
    definitions = case_definitions()
    revisions = {name: _repo(settings.cache_root / ("swe-bench" if name == "harness" else name)) for name in ("harness", "verified", "lite")}
    rows = [{"instance_id": c["instance_id"], "repo": c["repo"], "base_commit": c["base_commit"], "problem_statement": c["fixture_prompt"], "FAIL_TO_PASS": "[]", "PASS_TO_PASS": "[]"} for c in definitions]
    pq.write_table(pa.Table.from_pylist(rows), settings.cache_root / "verified" / "data" / "test.parquet")
    pq.write_table(pa.Table.from_pylist([{"instance_id": c["instance_id"]} for c in definitions[1:]]), settings.cache_root / "lite" / "data" / "test.parquet")
    for name in ("verified", "lite"):
        _git(settings.cache_root / name, "add", ".")
        _git(settings.cache_root / name, "commit", "-m", "data fixture")
        revisions[name] = _git(settings.cache_root / name, "rev-parse", "HEAD")
    lock = {"schema_version": "1.0", "sources": {name: {"url": f"https://example.invalid/{name}", "revision": rev} for name, rev in revisions.items()}}
    lock_path = tmp_path / "lock.json"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(EvalError, match="Lite.*missing"):
        prepare_data(settings, lock_path=lock_path, allow_fixture_hashes=True)
