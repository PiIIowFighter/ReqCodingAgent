from __future__ import annotations

REPLAY_STATUSES = frozenset({"passed", "test_failed", "infra_failed", "timeout", "invalid"})
TEST_OUTCOMES = frozenset({"PASSED", "FAILED", "ERROR", "SKIPPED", "XFAIL"})
