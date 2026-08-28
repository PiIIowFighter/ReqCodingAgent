from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from evalsys.config import Settings
from evalsys.data import PreparedData, prepare_data
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
    _git(path, "remote", "add", "origin", f"https://example.invalid/{path.name.replace('swe-bench', 'harness')}")
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
    monkeypatch.setattr("evalsys.data.transform_prompt", lambda _instance_id, prompt: prompt + "\nFUZZY")
    monkeypatch.setattr("evalsys.validation.transform_prompt", lambda _instance_id, prompt: prompt + "\nFUZZY")
    revisions = {}
    for name in ("harness", "verified", "lite"):
        revisions[name] = _repo(settings.cache_root / ("swe-bench" if name == "harness" else name))
    rows = []
    for case in definitions:
        # Fixture prompts intentionally use the frozen source text supplied by each definition.
        prompt = f"{case['instance_id']}\n{case['source_evidence']}"
        rows.append({
            "instance_id": case["instance_id"], "repo": case["repo"],
            "base_commit": case["base_commit"], "problem_statement": prompt,
            "patch": "SECRET PATCH", "test_patch": "SECRET TEST PATCH", "hints_text": "SECRET HINT",
            "FAIL_TO_PASS": json.dumps([f"{case['instance_id']}::fails"]),
            "PASS_TO_PASS": json.dumps([f"{case['instance_id']}::stable"]),
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


def test_t_o4_lf_prompt_removes_only_draggable_trigger_line():
    from evalsys.frozen_cases import transform_prompt

    original = (
        "[Bug]: Unable to pickle figure with draggable legend\n"
        "### Bug summary\n"
        "I am unable to pickle figure with draggable legend. Same error comes for draggable annotations.\n"
        "### Code for reproduction\n"
        "leg=ax.legend()\n"
        "leg.set_draggable(True) #pickling works after removing this line \n"
        "\n"
        "pickle.dumps(fig)\n"
        "plt.show()\n"
    )
    fuzzy = transform_prompt("matplotlib__matplotlib-25311", original)
    assert "leg.set_draggable(True)" not in fuzzy
    assert "pickling works after removing this line" not in fuzzy
    assert "leg=ax.legend()\n\npickle.dumps(fig)\nplt.show()\n" in fuzzy


def test_t_o3_removes_forbidden_hint_but_preserves_required_context():
    from evalsys.frozen_cases import transform_prompt

    original = (
        "skipping: --runxfail breaks pytest.mark.skip location reporting\n"
        "@pytest.mark.skip\ndef test_skip_location(): pass\n"
        "SKIPPED [1] test_it.py:3: unconditional skip\n"
        "SKIPPED [1] src/_pytest/skipping.py:238: unconditional skip\n"
        "The --runxfail is only about xfail and should not affect this at all.\n"
        "\n---\n\nHint: the bug is in `src/_pytest/skipping.py`, the `pytest_runtest_makereport` hook.\n"
    )
    fuzzy = transform_prompt("pytest-dev__pytest-7432", original)
    assert "Hint:" not in fuzzy
    assert "pytest_runtest_makereport" not in fuzzy
    assert "@pytest.mark.skip" in fuzzy
    assert "test_it.py:3" in fuzzy
    assert "一个与 xfail 相关的额外执行选项" in fuzzy


def test_generated_fuzzy_manifest_has_no_t_o3_or_t_o4_leaks():
    records = validate_jsonl(Path(__file__).parents[1] / "benchmark/manifests/paired-cases.jsonl", "public-case")
    fuzzy = {record["instance_id"]: record["prompt"] for record in records if record["prompt_variant"] == "fuzzy"}
    assert "leg.set_draggable(True)" not in fuzzy["matplotlib__matplotlib-25311"]
    assert "pickling works after removing this line" not in fuzzy["matplotlib__matplotlib-25311"]
    for forbidden in ("Hint:", "pytest_runtest_makereport"):
        assert forbidden not in fuzzy["pytest-dev__pytest-7432"]


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


def _real_prepared() -> PreparedData:
    root = Path(__file__).parents[1]
    cache = Path.home() / ".cache" / "reqcodingagent"
    source_rows = {row["instance_id"]: row for row in pq.read_table(cache / "verified/data/test-00000-of-00001.parquet").to_pylist() if row["instance_id"] in CASE_IDS}
    lite_ids = {row["instance_id"] for row in pq.read_table(cache / "lite/data/test-00000-of-00001.parquet", columns=["instance_id"]).to_pylist()}
    return PreparedData(root / "benchmark/manifests/paired-cases.jsonl", root / "benchmark/private/oracles.jsonl", source_rows, lite_ids, {"verified": "78f471bf655a3137b2e8a75af1501690ec009ec3"})


def test_validation_rejects_frozen_binding_and_source_test_tampering(tmp_path: Path):
    prepared = _real_prepared()
    records = validate_jsonl(prepared.public_manifest, "public-case")
    oracle_records = validate_jsonl(prepared.oracle_manifest, "oracle")

    def attack(mutator, expected: str):
        attacked = json.loads(json.dumps(records))
        mutator(attacked)
        public = tmp_path / "attacked.jsonl"
        public.write_text("".join(json.dumps(item) + "\n" for item in attacked), encoding="utf-8")
        candidate = PreparedData(public, prepared.oracle_manifest, prepared.source_rows, prepared.lite_ids, prepared.lock_heads)
        with pytest.raises(EvalError, match=expected):
            validate_benchmark(candidate)

    attack(lambda rows: rows.__setitem__(0, {**rows[0], "case_id": "T-O2-full"}), "case_id")
    attack(lambda rows: rows.__setitem__(1, {**rows[1], "hidden_fact_id": "T-O2-HF1"}), "metadata")
    attack(lambda rows: rows.__setitem__(1, {**rows[1], "prompt": rows[1]["prompt"] + "tamper", "prompt_sha256": __import__("hashlib").sha256((rows[1]["prompt"] + "tamper").encode()).hexdigest()}), "fuzzy prompt")
    attack(lambda rows: [row.update(split="dev") for row in rows if row["instance_id"] == "astropy__astropy-14995" and row["prompt_variant"] == "fuzzy"], "official task fields differ")
    attack(lambda rows: [row.update(FAIL_TO_PASS=[]) for row in rows if row["instance_id"] == "astropy__astropy-14995"], "FAIL_TO_PASS")
    attack(lambda rows: [row.update(source_revision="0" * 40) for row in rows if row["instance_id"] == "astropy__astropy-14995"], "source_revision")
    attack(lambda rows: [row.update(repo="wrong/repo") for row in rows if row["instance_id"] == "astropy__astropy-14995"], "repo")

    changed_oracles = json.loads(json.dumps(oracle_records))
    changed_oracles[0]["oracle_answer"] = "different but schema-valid"
    oracle = tmp_path / "oracle.jsonl"
    oracle.write_text("".join(json.dumps(item) + "\n" for item in changed_oracles), encoding="utf-8")
    with pytest.raises(EvalError, match="Oracle binding"):
        validate_benchmark(PreparedData(prepared.public_manifest, oracle, prepared.source_rows, prepared.lite_ids, prepared.lock_heads))


def test_lock_verification_rejects_wrong_remote(tmp_path: Path):
    settings = _settings(tmp_path)
    settings.project_root.mkdir()
    revisions = {name: _repo(settings.cache_root / ("swe-bench" if name == "harness" else name)) for name in ("harness", "verified", "lite")}
    _git(settings.cache_root / "lite", "remote", "set-url", "origin", "https://example.invalid/fork")
    lock = {"schema_version": "1.0", "sources": {name: {"url": f"https://example.invalid/{name}", "revision": rev} for name, rev in revisions.items()}}
    lock_path = tmp_path / "lock.json"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(EvalError, match="remote mismatch"):
        verify_source_locks(settings, lock_path)


def test_prepare_rejects_missing_lite_member(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = _settings(tmp_path)
    settings.project_root.mkdir()
    definitions = case_definitions()
    monkeypatch.setattr("evalsys.data.transform_prompt", lambda _instance_id, prompt: prompt + "\nFUZZY")
    revisions = {name: _repo(settings.cache_root / ("swe-bench" if name == "harness" else name)) for name in ("harness", "verified", "lite")}
    rows = [{"instance_id": c["instance_id"], "repo": c["repo"], "base_commit": c["base_commit"], "problem_statement": f"{c['instance_id']}\n{c['source_evidence']}", "FAIL_TO_PASS": "[]", "PASS_TO_PASS": "[]"} for c in definitions]
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
