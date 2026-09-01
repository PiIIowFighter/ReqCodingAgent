from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_iteration3_final_manifest_matches_frozen_sources():
    manifest = json.loads((ROOT / "audit/iteration3/final-results.json").read_text(encoding="utf-8"))
    report = ROOT / "audit/iteration3/reports/baseline-v3.json"
    ontology = ROOT / "configs/frozen/baseline-v3/requirement-ontology.json"
    plan = ROOT / "configs/frozen/baseline-v3/plan.json"
    assert manifest["active_cells"] == len(json.loads(report.read_text(encoding="utf-8"))["cells"]) == 24
    assert manifest["report_sha256"] == hashlib.sha256(report.read_bytes()).hexdigest()
    assert manifest["ontology_sha256"] == hashlib.sha256(ontology.read_bytes()).hexdigest()
    assert manifest["frozen_plan_sha256"] == hashlib.sha256(plan.read_bytes()).hexdigest()
    assert manifest["results"] == {"full": {"resolved": 10, "total": 12}, "fuzzy": {"resolved": 10, "total": 12}}
    assert manifest["experiment_labels"]["specification"] == {"E3": "fuzzy", "E4": "full"}
    assert manifest["experiment_labels"]["executed_frozen_plan"] == {"E3": "full", "E4": "fuzzy"}
