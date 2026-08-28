from __future__ import annotations

from pathlib import Path


def test_planning_revision_has_no_single_commit_gate():
    root = Path(__file__).resolve().parents[1]
    implementation = (root / "计划/第一次迭代-可验证评测环境实施规范.md").read_text(encoding="utf-8")
    strategy = (root / "计划/项目迭代与提交策略.md").read_text(encoding="utf-8")
    assert "文档版本：1.3" in implementation
    assert "功能迭代与 Git 提交不是一一对应关系" in implementation
    forbidden = (
        "只有全部满足才可创建第一次提交",
        "第 19 节验收全部通过后创建第一次提交",
        "对应 GitHub 提交：第一次提交",
    )
    assert not any(text in implementation for text in forbidden)
    second = (root / "计划/第二次迭代-完整基础CodingAgent实施规范.md").read_text(encoding="utf-8")
    readme = (root / "README.txt").read_text(encoding="utf-8")
    assert "功能迭代与 Git 提交不是一一对应关系" in strategy
    assert "不得 rebase、squash、amend、force push" in strategy
    assert 'pytest -m "not integration"' in implementation
    assert "optional full profile" in implementation
    assert "1×2 smoke" in strategy
    assert "全量 30 次 replay 不是前置条件" in second
    for forbidden_path in ("主项目仓库", "Git 历史", "benchmark/private", "计划/", "资料/", "evaluator"):
        assert forbidden_path in second
    assert 'pytest -m "not integration"' in readme
    assert "optional full profile" in readme
    assert len(readme) <= 1000
