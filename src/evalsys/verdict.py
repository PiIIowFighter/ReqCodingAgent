from __future__ import annotations


def decide_verdict(mode: str, outcomes: dict[str, str], fail_to_pass: list[str], pass_to_pass: list[str]) -> dict:
    expected = fail_to_pass + pass_to_pass
    if mode not in {"noop", "gold"} or any(test not in outcomes for test in expected):
        return {"status": "invalid", "reason": "missing or invalid per-test result"}
    passing = {"PASSED", "XFAIL", "SKIPPED"}
    f2p = {test: outcomes[test] for test in fail_to_pass}
    p2p = {test: outcomes[test] for test in pass_to_pass}
    if mode == "noop":
        ok = all(value in {"FAILED", "ERROR"} for value in f2p.values()) and all(value in passing for value in p2p.values())
    else:
        ok = all(value in {"PASSED", "XFAIL"} for value in f2p.values()) and all(value in passing for value in p2p.values())
    return {"status": "passed" if ok else "test_failed", "fail_to_pass": f2p, "pass_to_pass": p2p}
