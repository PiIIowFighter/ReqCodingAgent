from __future__ import annotations

import hashlib
from collections import Counter

from .errors import EvalError
from .frozen_cases import CASE_IDS, build_oracles, case_definitions, transform_prompt
from .schema import validate_jsonl


def _contains_han(text: str) -> bool:
    return any("㐀" <= char <= "䶿" or "一" <= char <= "鿿" for char in text)


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
    immutable = ("instance_id", "repo", "base_commit", "FAIL_TO_PASS", "PASS_TO_PASS", "source_revision", "original_prompt_sha256", "split", "pair_id")
    for pair_id, pair in grouped.items():
        if len(pair) != 2 or {r["prompt_variant"] for r in pair} != {"full", "fuzzy"}:
            raise EvalError(f"Pair {pair_id} must contain one full and one fuzzy record")
        if any(pair[0][key] != pair[1][key] for key in immutable):
            raise EvalError(f"Paired official task fields differ for {pair_id}")
        full = next(r for r in pair if r["prompt_variant"] == "full")
        fuzzy = next(r for r in pair if r["prompt_variant"] == "fuzzy")
        if _contains_han(full["prompt"]) != _contains_han(fuzzy["prompt"]):
            raise EvalError(f"Frozen pair language mismatch for {pair_id}")
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
    by_variant = {(record["instance_id"], record["prompt_variant"]): record for record in records}
    by_id = {instance_id: by_variant[(instance_id, "full")] for instance_id in CASE_IDS}
    for instance_id in CASE_IDS:
        definition = definitions[instance_id]
        full, fuzzy = by_variant[(instance_id, "full")], by_variant[(instance_id, "fuzzy")]
        row = prepared.source_rows.get(instance_id)
        if not row or full["prompt"] != row.get("problem_statement"):
            raise EvalError(f"Full prompt is not verbatim Verified text for {instance_id}")
        for field in ("repo", "base_commit"):
            if full[field] != definition[field] or fuzzy[field] != definition[field] or row.get(field) != definition[field]:
                raise EvalError(f"Frozen {field} binding mismatch for {instance_id}")
        for field in ("FAIL_TO_PASS", "PASS_TO_PASS"):
            authoritative = row.get(field)
            if isinstance(authoritative, str):
                import json
                authoritative = json.loads(authoritative)
            if full[field] != authoritative or fuzzy[field] != authoritative:
                raise EvalError(f"Verified {field} binding mismatch for {instance_id}")
        common_expected = {"pair_id": definition["pair_id"], "split": definition["split"], "source_revision": prepared.lock_heads["verified"], "approval_status": "frozen"}
        for variant, record in (("full", full), ("fuzzy", fuzzy)):
            for field, expected_value in common_expected.items():
                if record[field] != expected_value:
                    raise EvalError(f"Frozen {field} binding mismatch for {instance_id}/{variant}")
            if record["case_id"] != f"{definition['pair_id']}-{variant}":
                raise EvalError(f"Frozen case_id binding mismatch for {instance_id}/{variant}")
            actual_prompt_hash = hashlib.sha256(record["prompt"].encode("utf-8")).hexdigest()
            if record["prompt_sha256"] != actual_prompt_hash:
                raise EvalError(f"Prompt hash validation failed for {instance_id}/{variant}")
        expected_hash = hashlib.sha256(full["prompt"].encode("utf-8")).hexdigest() if allow_fixture_hashes else definition["original_prompt_sha256"]
        if full["original_prompt_sha256"] != expected_hash or fuzzy["original_prompt_sha256"] != expected_hash:
            raise EvalError(f"Original prompt hash binding failed for {instance_id}")
        full_metadata = (full["ambiguity_type"], full["severity"], full["transformation_description"], full["hidden_fact_id"])
        if full_metadata != (None, None, None, None):
            raise EvalError(f"Full variant fuzzy metadata must be null for {instance_id}")
        fuzzy_metadata = (fuzzy["ambiguity_type"], fuzzy["severity"], fuzzy["transformation_description"], fuzzy["hidden_fact_id"])
        expected_metadata = (definition["ambiguity_type"], definition["severity"], definition["transformation_description"], definition["hidden_fact_id"])
        if fuzzy_metadata != expected_metadata:
            raise EvalError(f"Frozen fuzzy metadata mismatch for {instance_id}")
        canonical_fuzzy = transform_prompt(instance_id, full["prompt"])
        if fuzzy["prompt"] != canonical_fuzzy:
            raise EvalError(f"Frozen fuzzy prompt mismatch for {instance_id}")
        if instance_id not in prepared.lite_ids:
            raise EvalError(f"Lite membership missing for {instance_id}")
    canonical_oracles = {item["instance_id"]: item for item in build_oracles()}
    for oracle in oracles:
        expected_oracle = canonical_oracles[oracle["instance_id"]]
        if oracle != expected_oracle:
            raise EvalError(f"Frozen Oracle binding mismatch for {oracle['instance_id']}")
        prompt = by_id[oracle["instance_id"]]["prompt"]
        if oracle["source_evidence"] not in prompt:
            raise EvalError(f"Oracle source_evidence is not a direct substring of the official prompt for {oracle['instance_id']}")
    receipt = {
        "schema_version": "1.0", "status": "passed",
        "checks": {"schema_pairs_12_3": True, "distribution_4_4_4_1_1_1": True, "official_hashes_15": True, "pair_official_fields_equal": True, "pair_language_consistent": True},
        "source_heads": dict(prepared.lock_heads),
        "inputs": {"public_manifest_sha256": hashlib.sha256(prepared.public_manifest.read_bytes()).hexdigest(), "oracle_manifest_sha256": hashlib.sha256(prepared.oracle_manifest.read_bytes()).hexdigest()},
    }
    return report | {"records": len(records), "oracles": len(oracles), "source_heads": prepared.lock_heads, "validation_receipt": receipt}
