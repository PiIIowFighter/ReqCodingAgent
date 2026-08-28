from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
import re

from .errors import EvalError

_DEFINITIONS = Path(__file__).resolve().parents[2] / "benchmark" / "manifests" / "case-definitions.json"


def case_definitions() -> list[dict]:
    cases = json.loads(_DEFINITIONS.read_text(encoding="utf-8"))["cases"]
    return cases


CASE_IDS = tuple(case["instance_id"] for case in case_definitions())


_CORE = {
    "django__django-13933": "ModelChoiceField and ModelMultipleChoiceField report invalid choices inconsistently: one includes the rejected value, while the other only says that the choice is unavailable. Make the field include this value in the invalid_choice error and its parameters.",
    "psf__requests-2317": "Neutronclient passes the request method and other request parameters through safe_encode_list. One of the resulting byte values is then converted by Requests into a literal string with a b'...' prefix, causing a 404 response. Normalize that parameter to the corresponding native string.",
    "scikit-learn__scikit-learn-13779": "A VotingClassifier contains two base estimators named lr and rf. After one component is set to None, fitting it with sample_weight still attempts to call fit on that object and raises an exception. The disabled component should be ignored correctly.",
    "sphinx-doc__sphinx-8721": "After configuring viewcode_enable_epub=False and running make html epub, both builders run, but one output still generates viewcode module pages. That builder should not generate these pages, while the other builder should retain its existing behavior.",
}


def _replace(text: str, old: str, new: str, instance_id: str) -> str:
    if old not in text:
        raise EvalError(f"Frozen transformation anchor missing for {instance_id}", hint=f"Expected source text containing {old!r}")
    return text.replace(old, new, 1)


def transform_prompt(instance_id: str, original: str) -> str:
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
        return text[:pstart] + "#### Problem Description\nA group of variables must first be stacked and then restored, but this roundtrip raises the MergeError shown above.\n\n" + text[pend:]
    if instance_id == "pytest-dev__pytest-7432":
        text = original.replace(
            "However, adding `pytest -rs --runxfail` breaks this:",
            "However, enabling an additional xfail-related execution option changes the reported location to:",
        )
        text = text.replace("--runxfail", "an additional xfail-related execution option").replace("skipping: an additional xfail-related execution option breaks", "skipping: an additional execution option breaks")
        hint = re.search(r"(?m)^---\r?$", text)
        return text[:hint.start()].rstrip() + "\n" if hint else text
    if instance_id == "matplotlib__matplotlib-25311":
        text = original.replace("[Bug]: Unable to pickle figure with draggable legend", "[Bug]: Unable to pickle figure after enabling an interactive legend behavior")
        text = _replace(text, "I am unable to pickle figure with draggable legend. Same error comes for draggable annotations.", "The Figure cannot be serialized after enabling an interactive legend behavior; draggable annotations exhibit a similar problem.", instance_id)
        line = r"(?m)^leg\.set_draggable\(True\)[^\r\n]*(?:\r?\n|$)"
        if not re.search(line, text):
            raise EvalError(f"Frozen transformation anchor missing for {instance_id}", hint="Expected the draggable trigger line")
        return re.sub(line, "", text, count=1)
    if instance_id == "django__django-10914":
        return original.replace("Set default FILE_UPLOAD_PERMISSION to 0o644.", "Set a consistent default FILE_UPLOAD_PERMISSIONS value。", 1)
    if instance_id == "matplotlib__matplotlib-23476":
        text = original.replace("[Bug]: DPI of a figure is doubled after unpickling on M1 Mac", "[Bug]: DPI of a figure becomes incorrect after unpickling on M1 Mac", 1)
        text = text.replace("When a figure is unpickled, it's dpi is doubled. This behaviour happens every time and if done in a loop it can cause an `OverflowError`.", "After repeated pickle/unpickle operations, the DPI grows incorrectly and eventually causes an `OverflowError`.", 1)
        text = text.replace("When a figure is unpickled, it's dpi is doubled.", "After unpickling, the figure DPI becomes incorrect.", 1)
        a = text.index("### Actual outcome")
        b = text.index("### Expected outcome", a)
        text = text[:a] + "### Actual outcome\n\nThe DPI increases on every round and eventually overflows.\n\n" + text[b:]
        a = text.index("### Expected outcome")
        b = text.index("### Additional information", a)
        return text[:a] + "### Expected outcome\n\nThe DPI should always retain the correct value from the original Figure.\n\n" + text[b:]
    if instance_id == "scikit-learn__scikit-learn-13439":
        text = original.replace("Pipeline should implement __len__", "Pipeline should expose its number of steps through a standard interface", 1)
        text = _replace(text, "With the new indexing support `pipe[:len(pipe)]` raises an error.", "Querying the Pipeline size through the expected standard Python interface raises an error.", instance_id)
        return text.replace("len(pipe)", "# Query the number of steps using the intended standard interface", 1)
    if instance_id == "sphinx-doc__sphinx-8595":
        text = original.replace("autodoc: empty __all__ attribute is ignored", "autodoc mishandles an explicitly defined __all__ attribute")
        text = text.replace("empty `__all__` attribute is ignored", "an explicitly defined `__all__` attribute is mishandled")
        text = text.replace("__all__ = []", "# __all__ is explicitly defined to a particular value", 1)
        return text.replace("No entries should be shown because `__all__` is empty.", "autodoc should respect the explicit export list.", 1)
    if instance_id == "django__django-11133":
        return "HttpResponse incorrectly serializes some database binary values.\nWhen database BinaryField content is written to an HttpResponse, the value returned by SQLite works correctly, but the same content returned by PostgreSQL becomes a representation like b'<... at 0x...>' instead of b'My Content'. String and ordinary bytes inputs both work correctly."
    if instance_id == "scikit-learn__scikit-learn-14983":
        a = original.index("#### Expected Results")
        b = original.index("#### Actual Results", a)
        return original[:a] + "#### Expected Results\n\nBoth objects should display stable, readable constructor-style representations that include their cross-validation configuration, rather than default object addresses.\n\n" + original[b:]
    if instance_id == "matplotlib__matplotlib-25332":
        text = original.replace("[Bug]: Unable to pickle figure with aligned labels", "A figure fails after combining label alignment and serialization", 1)
        text = text.replace("Unable to pickle figure after calling `align_labels()`", "A Figure performs label alignment and serialization; together they raise the reported error.", 1)
        return text.replace(" ##pickling works after removing this line ", "")
    raise EvalError(f"No frozen transformation is implemented for instance_id: {instance_id}")


def build_oracles() -> list[dict]:
    result = []
    for case in case_definitions():
        result.append({key: deepcopy(case[key]) for key in ("case_id", "pair_id", "instance_id", "hidden_fact_id", "hidden_fact_category", "source_evidence", "oracle_answer", "acceptable_intents", "ambiguity_type", "severity", "approval_status")} | {"schema_version": "1.0", "ontology_mapping": None})
    return result
