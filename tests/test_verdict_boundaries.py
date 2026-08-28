from __future__ import annotations

import pytest

from evalsys.verdict import decide_verdict


@pytest.mark.parametrize("status", ["SKIPPED", "XFAIL"])
def test_pass_to_pass_requires_real_pass(status: str):
    verdict = decide_verdict("gold", {"fixed": "PASSED", "stable": status}, ["fixed"], ["stable"])
    assert verdict["status"] == "test_failed"
    assert verdict["classification"] == "unexpected_test_status"


@pytest.mark.parametrize("status", ["SKIPPED", "XFAIL"])
def test_gold_fail_to_pass_requires_real_pass(status: str):
    assert decide_verdict("gold", {"fixed": status}, ["fixed"], [])["status"] == "test_failed"


@pytest.mark.parametrize("status", ["SKIPPED", "XFAIL", "PASSED"])
def test_noop_fail_to_pass_requires_real_failure(status: str):
    assert decide_verdict("noop", {"fixed": status}, ["fixed"], [])["status"] == "test_failed"


def test_error_is_invalid_not_success():
    verdict = decide_verdict("noop", {"fixed": "ERROR"}, ["fixed"], [])
    assert verdict == {"status": "invalid", "classification": "test_error", "fail_to_pass": {"fixed": "ERROR"}, "pass_to_pass": {}}
