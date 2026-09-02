from __future__ import annotations

import json
from pathlib import Path

import pytest

from demo_gui.prepare_stock_demo import prepare
from demo_gui.interview import InterviewSession
from demo_gui.server import load_ontology


def test_prepare_stock_demo_initializes_clean_git_and_rejects_nonempty(tmp_path: Path):
    target = tmp_path / "stock"
    prepare(target)
    assert (target / ".git").is_dir()
    assert json.loads((target / "stocks.json").read_text(encoding="utf-8"))[0]["code"]
    with pytest.raises(ValueError, match="missing or empty"):
        prepare(target)
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="missing or empty"):
        prepare(occupied)
    assert (occupied / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_stock_scenario_is_frozen_slot_subset():
    frozen = load_ontology()
    scenario = load_ontology(scenario="stock-search")["scenario"]
    frozen_slots = {slot["id"] for category in frozen["tree"]["children"] for slot in category["children"]}
    assert scenario["overlay_only"] is True
    assert scenario["layer"] == "presentation_overlay"
    assert scenario["ontology_effect"] == "none"
    assert set(scenario["slots"]) == frozen_slots
    assert scenario["min_turns"] == scenario["max_turns"] == 3


def test_frozen_ontology_is_domain_neutral_and_presentation_profile_does_not_prefill_stock_facts():
    frozen_path = Path("configs/frozen/baseline-v3/requirement-ontology.json")
    frozen_text = frozen_path.read_text(encoding="utf-8").lower()
    assert "stock" not in frozen_text
    assert "股票" not in frozen_text

    session = InterviewSession("为现有网页增加搜索功能", object(), "test", scenario="stock-search")
    system_prompt = session.messages[0].text.lower()
    assert session.min_turns == session.max_turns == 3
    assert "stock-search" not in system_prompt
    assert "test_site.sh" not in system_prompt
    assert "do not prefill domain facts" in system_prompt
