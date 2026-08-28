from __future__ import annotations

from collections import Counter

from .errors import EvalError


def validate_pairs(records: list[dict]) -> dict:
    grouped: dict[str, list[dict]] = {}
    for record in records:
        grouped.setdefault(record["pair_id"], []).append(record)
    test = [pair for pair in grouped.values() if pair and pair[0]["split"] == "test"]
    dev = [pair for pair in grouped.values() if pair and pair[0]["split"] == "dev"]
    if len(test) != 12 or len(dev) != 3:
        raise EvalError(f"Expected 12 test pairs and 3 dev pairs, got {len(test)} and {len(dev)}")
    distributions = {
        "test": Counter(next(r["ambiguity_type"] for r in pair if r["prompt_variant"] == "fuzzy") for pair in test),
        "dev": Counter(next(r["ambiguity_type"] for r in pair if r["prompt_variant"] == "fuzzy") for pair in dev),
    }
    if sorted(distributions["test"].values()) != [4, 4, 4] or sorted(distributions["dev"].values()) != [1, 1, 1]:
        raise EvalError("Invalid ambiguity distribution")
    immutable = ("instance_id", "repo", "base_commit", "FAIL_TO_PASS", "PASS_TO_PASS")
    for pair in grouped.values():
        if len(pair) != 2 or {r["prompt_variant"] for r in pair} != {"full", "fuzzy"}:
            raise EvalError("Every pair must contain one full and one fuzzy record")
        if any(pair[0][key] != pair[1][key] for key in immutable):
            raise EvalError("Paired official task fields differ")
    return {"test_pairs": 12, "dev_pairs": 3, "distributions": {k: dict(v) for k, v in distributions.items()}}
