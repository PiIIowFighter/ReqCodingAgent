"""Deterministic offline stock-search walkthrough using the real tool registry."""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from demo_gui.prepare_stock_demo import prepare
from reqagent.adaptive import route_task
from reqagent.config import AgentConfig
from reqagent.tools import build_registry
from reqagent.tools.command import LocalTestCommandExecutor
from reqagent.workspace import GitWorkspace


TASK = "为这个现有的静态前端项目增加股票搜索功能"
PATCH = """*** Begin Patch
*** Update File: index.html
@@
       <div class=\"panel-heading\"><h2 id=\"list-title\">股票列表</h2><span id=\"result-count\" class=\"muted\"></span></div>
+      <label class=\"search-label\" for=\"stock-search\">搜索股票</label><input id=\"stock-search\" type=\"search\" placeholder=\"输入代码或名称\">
       <div id=\"stock-list\" class=\"stock-list\" aria-live=\"polite\"></div>
*** Update File: app.js
@@
-  .then(stocks => renderStocks(stocks));
+  .then(stocks => { renderStocks(stocks); document.querySelector(\"#stock-search\").addEventListener(\"input\", event => { const query = event.target.value.trim().toLowerCase(); renderStocks(stocks.filter(stock => !query || stock.code.includes(query) || stock.name.toLowerCase().includes(query))); }); });
*** Update File: app.js
@@
-  resultCount.textContent = `${stocks.length} 只`;
+  resultCount.textContent = `${stocks.length} 只`;
+  if (!stocks.length) { stockList.innerHTML = '<p class=\"muted\">没有匹配结果</p>'; return; }
   stockList.innerHTML = stocks.map(stock => `
*** End Patch"""


def run(target: Path | None = None) -> dict:
    with tempfile.TemporaryDirectory(prefix="stock-demo-offline-") as temp:
        project = target or Path(temp) / "project"
        prepare(project)
        config = AgentConfig.load(PROJECT_ROOT / "configs/agent/offline-scripted.json")
        workspace = GitWorkspace.create(project, in_place=True)
        registry = build_registry(workspace, config.raw, command_executor=LocalTestCommandExecutor(), artifact_dir=project / ".commands", requirement_refinement="auto", task=TASK)
        if route_task(TASK).mode != "refine":
            raise AssertionError("stock demo must exercise refine route")
        calls = []
        for name, args in (("list_files", {"path": ".", "depth": 2}), ("read_file", {"path": "index.html"}), ("read_file", {"path": "app.js"}), ("search_text", {"query": "stock", "path": "."})):
            result = registry.execute(name, args); assert result.ok, result.error; calls.append(name)
        evidence = list(registry.adaptive.evidence)
        registry.adaptive.transition_to_synthesis()
        brief = {"ambiguity_reason": "搜索行为和验证入口需从现有文件确认", "chosen_interpretation": "增加本地股票代码和名称搜索", "targets": ["index.html", "app.js"], "expected_behavior": "输入代码或名称过滤列表，空结果有提示", "regression_invariants": ["保留本地数据和原生页面"], "validation_plan": ["sh test_site.sh"], "unresolved_uncertainty": [], "evidence_ids": evidence, "candidates": [{"interpretation": "增加本地股票代码和名称搜索", "task_fit": 4, "repository_support": 4, "compatibility": 4, "testability": 4}]}
        result = registry.execute("record_requirement_brief", brief); assert result.ok, result.error; calls.append("record_requirement_brief")
        result = registry.execute("apply_patch", {"patch": PATCH}); assert result.ok, result.error; calls.append("apply_patch")
        result = registry.execute("run_command", {"command": "sh test_site.sh"}); assert result.ok and result.data.get("exit_code") == 0, result.error; calls.append("run_command")
        result = registry.execute("submit", {"summary": "Added local stock code/name search.", "tests": ["sh test_site.sh"], "limitations": "Uses simulated local data."}); assert result.ok; calls.append("submit")
        patch = workspace.diff()
        assert patch
        return {"route": "refine", "turns": 3, "tool_calls": calls, "stop_reason": "submitted", "patch_nonempty": bool(patch), "test": "sh test_site.sh"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path)
    print(json.dumps(run(parser.parse_args().target), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
