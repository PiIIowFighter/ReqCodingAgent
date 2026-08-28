from __future__ import annotations

from pathlib import Path


def test_planning_revision_has_no_single_commit_gate():
    root = Path(__file__).resolve().parents[1]
    implementation = (root / "计划/第一次迭代-可验证评测环境实施规范.md").read_text(encoding="utf-8")
    strategy = (root / "计划/项目迭代与提交策略.md").read_text(encoding="utf-8")
    assert "文档版本：1.2" in implementation
    assert "功能迭代与 Git 提交不是一一对应关系" in implementation
    forbidden = (
        "只有全部满足才可创建第一次提交",
        "第 19 节验收全部通过后创建第一次提交",
        "对应 GitHub 提交：第一次提交",
    )
    assert not any(text in implementation for text in forbidden)
    assert "功能迭代与 Git 提交不是一一对应关系" in strategy
    assert "不得 rebase、squash、amend、force push" in strategy
