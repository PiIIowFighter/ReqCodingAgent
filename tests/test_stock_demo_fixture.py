from __future__ import annotations

import json
from pathlib import Path

import pytest

from demo_gui.prepare_stock_demo import prepare
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
    assert set(scenario["slots"]) == frozen_slots
    assert scenario["min_turns"] == scenario["max_turns"] == 3
