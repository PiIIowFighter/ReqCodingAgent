from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from .errors import EvalError

_DEFINITIONS = Path(__file__).resolve().parents[2] / "benchmark" / "manifests" / "case-definitions.json"


def case_definitions() -> list[dict]:
    cases = json.loads(_DEFINITIONS.read_text(encoding="utf-8"))["cases"]
    # A self-contained harmless prompt lets tests construct tiny source fixtures.
    for case in cases:
        case["fixture_prompt"] = f"{case['instance_id']}\n{case['source_evidence']}"
        case["fixture_fail_to_pass"] = [f"{case['instance_id']}::fails"]
        case["fixture_pass_to_pass"] = [f"{case['instance_id']}::stable"]
    return cases


CASE_IDS = tuple(case["instance_id"] for case in case_definitions())


_CORE = {
    "django__django-13933": "ModelChoiceField 和 ModelMultipleChoiceField 对无效选择的报错不一致：一个会携带被拒绝的值，另一个只说明该选择不可用。请让该字段在 invalid_choice 错误及其参数中包含这个值。",
    "psf__requests-2317": "Neutronclient 会把请求方法及其他请求参数一起经过 safe_encode_list。随后，其中一个字节值在 Requests 中被转换成带 b'...' 前缀的字面字符串，导致请求返回 404。请让该参数被规范化为对应的原生字符串。",
    "scikit-learn__scikit-learn-13779": "一个 VotingClassifier 包含名为 lr 和 rf 的两个基础估计器。将其中一个组件设为 None 后，再使用 sample_weight 拟合它时，它仍尝试在该对象上调用 fit，从而触发异常。被禁用的组件应被正确忽略。",
    "sphinx-doc__sphinx-8721": "在配置 viewcode_enable_epub=False 后执行 make html epub，两个构建器都会运行，但其中一个输出仍然生成了 viewcode 模块页面。该构建器不应生成这些页面，另一个构建器则应保持原有行为。",
}


def _replace(text: str, old: str, new: str, instance_id: str) -> str:
    if old not in text:
        raise EvalError(f"Frozen transformation anchor missing for {instance_id}", hint=f"Expected source text containing {old!r}")
    return text.replace(old, new, 1)


def transform_prompt(instance_id: str, original: str) -> str:
    if original.startswith(instance_id + "\n"):
        return original + "\n[FROZEN FUZZY FIXTURE]"
    if instance_id in _CORE:
        return _CORE[instance_id]
    if instance_id == "astropy__astropy-14995":
        start = original.index("### Expected behavior")
        end = original.index("### How to Reproduce", start)
        return original[:start] + "### Expected behavior\n\nNo response\n\n" + original[end:]
    if instance_id == "pydata__xarray-4094":
        start = original.index("#### MCVE Code Sample")
        end = original.index("#### Expected Output", start)
        block = "#### MCVE Code Sample\n\n```python\nstacked = data.to_stacked_array('y', sample_dims=['x'])\nunstacked = stacked.to_unstacked_dataset('y')\n# MergeError: conflicting values for variable 'y' on objects to be combined.\n```\n\n"
        text = "to_unstacked_dataset roundtrip raises MergeError\n" + original.split("\n", 1)[1]
        text = text[:text.index("#### MCVE Code Sample")] + block + text[text.index("#### Expected Output"):]
        pstart = text.index("#### Problem Description")
        pend = text.index("#### Versions", pstart)
        return text[:pstart] + "#### Problem Description\n需要先堆叠一组变量，之后再还原，但该 roundtrip 会触发上述 MergeError。\n\n" + text[pend:]
    if instance_id == "pytest-dev__pytest-7432":
        text = original.replace("--runxfail", "一个与 xfail 相关的额外执行选项").replace("skipping: 一个与 xfail 相关的额外执行选项 breaks", "skipping: an additional execution option breaks")
        hint = text.find("\n---\n")
        return text[:hint].rstrip() + "\n" if hint >= 0 else text
    if instance_id == "matplotlib__matplotlib-25311":
        text = original.replace("[Bug]: Unable to pickle figure with draggable legend", "[Bug]: Unable to pickle figure after enabling an interactive legend behavior")
        text = _replace(text, "I am unable to pickle figure with draggable legend. Same error comes for draggable annotations.", "启用一种交互式 legend 行为后无法序列化 Figure；draggable annotation 也会出现类似问题。", instance_id)
        return text.replace("leg.set_draggable(True) #pickling works after removing this line \r\n", "")
    if instance_id == "django__django-10914":
        return original.replace("Set default FILE_UPLOAD_PERMISSION to 0o644.", "Set a consistent default FILE_UPLOAD_PERMISSIONS value。", 1)
    if instance_id == "matplotlib__matplotlib-23476":
        text = original.replace("[Bug]: DPI of a figure is doubled after unpickling on M1 Mac", "[Bug]: DPI of a figure becomes incorrect after unpickling on M1 Mac", 1)
        text = text.replace("When a figure is unpickled, it's dpi is doubled. This behaviour happens every time and if done in a loop it can cause an `OverflowError`.", "After repeated pickle/unpickle operations, the DPI grows incorrectly and eventually causes an `OverflowError`.", 1)
        text = text.replace("When a figure is unpickled, it's dpi is doubled.", "After unpickling, the figure DPI becomes incorrect.", 1)
        a = text.index("### Actual outcome")
        b = text.index("### Expected outcome", a)
        text = text[:a] + "### Actual outcome\n\nDPI 每轮都增大并最终越界。\n\n" + text[b:]
        a = text.index("### Expected outcome")
        b = text.index("### Additional information", a)
        return text[:a] + "### Expected outcome\n\nDPI 应始终保持原 Figure 的正确值。\n\n" + text[b:]
    if instance_id == "scikit-learn__scikit-learn-13439":
        text = original.replace("Pipeline should implement __len__", "Pipeline should expose its number of steps through a standard interface", 1)
        text = _replace(text, "With the new indexing support `pipe[:len(pipe)]` raises an error.", "通过预期的标准 Python 接口查询 Pipeline 大小时发生错误。", instance_id)
        return text.replace("len(pipe)", "# Query the number of steps using the intended standard interface", 1)
    if instance_id == "sphinx-doc__sphinx-8595":
        text = original.replace("autodoc: empty __all__ attribute is ignored", "autodoc mishandles an explicitly defined __all__ attribute")
        text = text.replace("empty `__all__` attribute is ignored", "an explicitly defined `__all__` attribute is mishandled")
        text = text.replace("__all__ = []", "# __all__ is explicitly defined to a particular value", 1)
        return text.replace("No entries should be shown because `__all__` is empty.", "autodoc 应尊重显式导出列表。", 1)
    if instance_id == "django__django-11133":
        return "HttpResponse incorrectly serializes some database binary values。\n将数据库 BinaryField 的内容写入 HttpResponse 时，SQLite 返回的值可以正常工作，但 PostgreSQL 返回的同一内容会变成类似 b'<... at 0x...>' 的表示，而不是 b'My Content'。字符串和普通字节输入均能正常工作。"
    if instance_id == "scikit-learn__scikit-learn-14983":
        a = original.index("#### Expected Results")
        b = original.index("#### Actual Results", a)
        return original[:a] + "#### Expected Results\n\n两个对象都应显示稳定、可读的构造器式表示，并包含其交叉验证配置，而不是默认对象地址。\n\n" + original[b:]
    if instance_id == "matplotlib__matplotlib-25332":
        text = original.replace("[Bug]: Unable to pickle figure with aligned labels", "A figure fails after combining label alignment and serialization", 1)
        text = text.replace("Unable to pickle figure after calling `align_labels()`", "A Figure performs label alignment and serialization; together they raise the reported error.", 1)
        return text.replace(" ##pickling works after removing this line ", "")
    # Tiny test fixtures still exercise generation without pretending to be official data.
    if instance_id in CASE_IDS:
        return original + "\n[FROZEN FUZZY FIXTURE]"
    raise EvalError(f"Unknown frozen instance_id: {instance_id}")


def build_oracles() -> list[dict]:
    result = []
    for case in case_definitions():
        result.append({key: deepcopy(case[key]) for key in ("case_id", "pair_id", "instance_id", "hidden_fact_id", "hidden_fact_category", "source_evidence", "oracle_answer", "acceptable_intents", "ambiguity_type", "severity", "approval_status")} | {"schema_version": "1.0", "ontology_mapping": None})
    return result
