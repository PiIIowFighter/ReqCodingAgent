from __future__ import annotations


def decide_verdict(mode: str, outcomes: dict[str, str], fail_to_pass: list[str], pass_to_pass: list[str]) -> dict:
    expected = fail_to_pass + pass_to_pass
    f2p = {test: outcomes[test] for test in fail_to_pass if test in outcomes}
    p2p = {test: outcomes[test] for test in pass_to_pass if test in outcomes}
    if mode not in {"noop", "gold"}:
        return {"status": "invalid", "classification": "invalid_mode", "fail_to_pass": f2p, "pass_to_pass": p2p}
    if len(f2p) != len(fail_to_pass) or len(p2p) != len(pass_to_pass):
        return {"status": "invalid", "classification": "missing_test_result", "fail_to_pass": f2p, "pass_to_pass": p2p}
    if any(outcomes[test] == "ERROR" for test in expected):
        return {"status": "invalid", "classification": "test_error", "fail_to_pass": f2p, "pass_to_pass": p2p}
    f2p_expected = "FAILED" if mode == "noop" else "PASSED"
    ok = all(value == f2p_expected for value in f2p.values()) and all(value == "PASSED" for value in p2p.values())
    return {
        "status": "passed" if ok else "test_failed",
        "classification": "expected_test_statuses" if ok else "unexpected_test_status",
        "fail_to_pass": f2p,
        "pass_to_pass": p2p,
    }
