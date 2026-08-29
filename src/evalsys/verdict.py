from __future__ import annotations


def decide_verdict(mode: str, outcomes: dict[str, str], fail_to_pass: list[str], pass_to_pass: list[str], *, failure_kind: str | None = None) -> dict:
    expected = fail_to_pass + pass_to_pass
    f2p = {test: outcomes[test] for test in fail_to_pass if test in outcomes}
    p2p = {test: outcomes[test] for test in pass_to_pass if test in outcomes}
    if mode == "agent" and failure_kind in {"patch_apply", "test_timeout", "test_error", "test_skipped", "unknown"}:
        classifications = {
            "patch_apply": "agent_patch_apply_failed",
            "test_timeout": "tests_timeout",
            "test_error": "tests_error",
            "test_skipped": "tests_skipped",
            "unknown": "tests_unresolved",
        }
        return {"status": "unresolved", "classification": classifications[failure_kind], "fail_to_pass": f2p, "pass_to_pass": p2p}
    if mode not in {"noop", "gold", "agent"}:
        return {"status": "invalid", "classification": "invalid_mode", "fail_to_pass": f2p, "pass_to_pass": p2p}
    if len(f2p) != len(fail_to_pass) or len(p2p) != len(pass_to_pass):
        return {"status": "invalid", "classification": "missing_test_result", "fail_to_pass": f2p, "pass_to_pass": p2p}
    if any(outcomes[test] == "ERROR" for test in expected):
        return {"status": "unresolved" if mode == "agent" else "invalid", "classification": "tests_error" if mode == "agent" else "test_error", "fail_to_pass": f2p, "pass_to_pass": p2p}
    if mode == "agent" and any(outcomes[test] == "SKIPPED" for test in expected):
        return {"status": "unresolved", "classification": "tests_skipped", "fail_to_pass": f2p, "pass_to_pass": p2p}
    f2p_expected = "FAILED" if mode == "noop" else "PASSED"
    ok = all(value == f2p_expected for value in f2p.values()) and all(value == "PASSED" for value in p2p.values())
    if mode == "agent":
        return {
            "status": "resolved" if ok else "unresolved",
            "classification": "resolved" if ok else "tests_failed",
            "fail_to_pass": f2p,
            "pass_to_pass": p2p,
        }
    return {
        "status": "passed" if ok else "test_failed",
        "classification": "expected_test_statuses" if ok else "unexpected_test_status",
        "fail_to_pass": f2p,
        "pass_to_pass": p2p,
    }
