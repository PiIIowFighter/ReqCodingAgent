from __future__ import annotations

import hashlib
from collections import Counter

from .errors import EvalError
from .frozen_cases import CASE_IDS, case_definitions
from .schema import validate_jsonl


def validate_pairs(records: list[dict]) -> dict:
    grouped: dict[str, list[dict]] = {}
    for record in records:
        grouped.setdefault(record["pair_id"], []).append(record)
    if len(grouped) != len(set(grouped)):
        raise EvalError("Duplicate pair_id")
    test = [pair for pair in grouped.values() if pair and pair[0]["split"] == "test"]
    dev = [pair for pair in grouped.values() if pair and pair[0]["split"] == "dev"]
    if len(test) != 12 or len(dev) != 3:
        raise EvalError(f"Expected 12 test pairs and 3 dev pairs, got {len(test)} and {len(dev)}")
    expected = {"omission", "specificity_reduction", "referential_ambiguity"}
    distributions = {split: Counter(next(r["ambiguity_type"] for r in pair if r["prompt_variant"] == "fuzzy") for pair in pairs) for split, pairs in (("test", test), ("dev", dev))}
    if distributions["test"] != Counter({kind: 4 for kind in expected}) or distributions["dev"] != Counter({kind: 1 for kind in expected}):
        raise EvalError(f"Invalid ambiguity distribution: {dict(distributions['test'])}, {dict(distributions['dev'])}")
    immutable = ("instance_id", "repo", "base_commit", "FAIL_TO_PASS", "PASS_TO_PASS", "source_revision", "original_prompt_sha256")
    for pair_id, pair in grouped.items():
        if len(pair) != 2 or {r["prompt_variant"] for r in pair} != {"full", "fuzzy"}:
            raise EvalError(f"Pair {pair_id} must contain one full and one fuzzy record")
        if any(pair[0][key] != pair[1][key] for key in immutable):
            raise EvalError(f"Paired official task fields differ for {pair_id}")
        fuzzy = next(r for r in pair if r["prompt_variant"] == "fuzzy")
        if fuzzy["severity"] != "medium" or not fuzzy["transformation_description"] or not fuzzy["hidden_fact_id"] or fuzzy["approval_status"] != "frozen":
            raise EvalError(f"Fuzzy freeze metadata is incomplete for {pair_id}")
    if {pair[0]["instance_id"] for pair in grouped.values()} != set(CASE_IDS):
        raise EvalError("Manifest IDs do not equal the frozen 15-case allowlist")
    return {"test_pairs": 12, "dev_pairs": 3, "distributions": {key: dict(value) for key, value in distributions.items()}}


def validate_benchmark(prepared, *, allow_fixture_hashes: bool = False) -> dict:
    records = validate_jsonl(prepared.public_manifest, "public-case")
    oracles = validate_jsonl(prepared.oracle_manifest, "oracle")
    report = validate_pairs(records)
    definitions = {case["instance_id"]: case for case in case_definitions()}
    if len(oracles) != 15 or {item["instance_id"] for item in oracles} != set(CASE_IDS):
        raise EvalError("Oracle records must cover exactly the frozen 15 cases")
    by_id = {record["instance_id"]: record for record in records if record["prompt_variant"] == "full"}
    for instance_id, full in by_id.items():
        row = prepared.source_rows.get(instance_id)
        if not row or full["prompt"] != row.get("problem_statement"):
            raise EvalError(f"Full prompt is not verbatim Verified text for {instance_id}")
        actual = hashlib.sha256(full["prompt"].encode("utf-8")).hexdigest()
        expected = actual if allow_fixture_hashes else definitions[instance_id]["original_prompt_sha256"]
        if full["prompt_sha256"] != actual or full["original_prompt_sha256"] != expected:
            raise EvalError(f"Prompt hash validation failed for {instance_id}")
        if instance_id not in prepared.lite_ids:
            raise EvalError(f"Lite membership missing for {instance_id}")
    for oracle in oracles:
        prompt = by_id[oracle["instance_id"]]["prompt"]
        if oracle["source_evidence"] not in prompt:
            raise EvalError(f"Oracle source_evidence is not a direct substring of the official prompt for {oracle['instance_id']}")
        if oracle["ontology_mapping"] is not None and not isinstance(oracle["ontology_mapping"], dict):
            raise EvalError(f"ontology_mapping must be null or object for {oracle['instance_id']}")
    return report | {"records": len(records), "oracles": len(oracles), "source_heads": prepared.lock_heads}
