from __future__ import annotations

import threading
import time
from pathlib import Path

from evalsys.iteration3 import baseline_run_closure, run_serialized_evaluator


def test_agent_workers_overlap_while_evaluator_is_serial_and_releases_after_error(tmp_path: Path):
    agent_active = evaluator_active = 0
    agent_max = evaluator_max = 0
    lock = threading.Lock()
    barrier = threading.Barrier(2)
    results = []

    def agent(index: int):
        nonlocal agent_active, agent_max, evaluator_active, evaluator_max
        with lock:
            agent_active += 1
            agent_max = max(agent_max, agent_active)
        barrier.wait()
        time.sleep(0.03)
        with lock:
            agent_active -= 1

        def evaluate():
            nonlocal evaluator_active, evaluator_max
            with lock:
                evaluator_active += 1
                evaluator_max = max(evaluator_max, evaluator_active)
            try:
                time.sleep(0.03)
                if index == 0:
                    raise RuntimeError("synthetic evaluator failure")
                return index
            finally:
                with lock:
                    evaluator_active -= 1

        try:
            value = run_serialized_evaluator(tmp_path, evaluate)
            results.append((index, value))
        except RuntimeError:
            results.append((index, "failed"))

    threads = [threading.Thread(target=agent, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert agent_max == 2
    assert evaluator_max == 1
    assert sorted(results) == [(0, "failed"), (1, 1)]
    assert not (tmp_path / ".evaluator.lock").exists()


def test_baseline_run_closure_does_not_mix_iteration3_baselines():
    runs = [
        {"run_id": "v2-active", "run_type": "formal_cell", "status": "resolved", "supersedes": ["v2-infra"]},
        {"run_id": "v2-infra", "run_type": "formal_cell", "status": "eval_infra_failed", "supersedes": []},
        {"run_id": "v3-active", "run_type": "formal_cell", "status": "unresolved", "supersedes": ["v3-infra"]},
        {"run_id": "v3-infra", "run_type": "formal_cell", "status": "eval_infra_failed", "supersedes": []},
    ]
    manifest = {"baseline": "baseline-v3", "cell_runs": {"cell": {"run_id": "v3-active", "state": "complete"}}}
    selected = baseline_run_closure(manifest, runs)
    assert {row["run_id"] for row in selected} == {"v3-active", "v3-infra"}
    assert all(not row["run_id"].startswith("v2") for row in selected)
