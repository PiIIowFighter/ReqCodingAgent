from demo_gui.stock_demo_offline import run


def test_stock_demo_offline_uses_real_tools_end_to_end():
    result = run()
    assert result["route"] == "refine"
    assert result["turns"] == 3
    assert result["stop_reason"] == "submitted"
    assert result["patch_nonempty"] is True
    assert result["tool_calls"] == ["list_files", "read_file", "read_file", "search_text", "record_requirement_brief", "apply_patch", "run_command", "submit"]
