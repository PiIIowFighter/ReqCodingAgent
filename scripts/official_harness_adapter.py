from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pgid-file", type=Path, required=True)
    parser.add_argument("--harness-checkout", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--predictions", required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--timeout", type=int, required=True)
    parser.add_argument("--max-workers", type=int, required=True)
    parser.add_argument("--label", action="append", default=[])
    parser.add_argument("--skip-patch", action="store_true")
    parser.add_argument("--frozen-image")
    return parser.parse_args()


def classify_artifacts(log_root: Path, instance_id: str, *, skip_patch: bool) -> dict:
    instance_logs = list(log_root.glob(f"*/{instance_id}/run_instance.log"))
    test_outputs = list(log_root.glob(f"*/{instance_id}/test_output.txt"))
    reports = list(log_root.glob(f"*/{instance_id}/report.json"))
    text = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in instance_logs + test_outputs + reports)
    if ">>>>> Patch Apply Failed" in text:
        return {"status": "invalid" if not skip_patch else "infra_failed", "classification": "official_patch_apply_failure", "stage": "patch", "message": "official harness patch application failed"}
    if ">>>>> Tests Timed Out" in text or re.search(r"Timeout error: \d+ seconds exceeded\.", text) or re.search(r"Test timed out after \d+ seconds\.", text):
        return {"status": "timeout", "classification": "official_tests_timeout", "stage": "tests", "message": "official harness test timeout"}
    if ">>>>> Tests Errored" in text:
        return {"status": "infra_failed", "classification": "official_tests_error", "stage": "tests", "message": "official harness tests errored"}
    if any(marker in text for marker in ("Image not found", "Error in evaluating model", "Docker")) and not test_outputs:
        return {"status": "infra_failed", "classification": "official_environment_failure", "stage": "environment", "message": text[-1000:]}
    if not instance_logs:
        return {"status": "infra_failed", "classification": "missing_instance_log", "stage": "environment", "message": "official harness produced no instance log"}
    return {"status": "invalid", "classification": "missing_test_output", "stage": "tests", "message": "official harness produced no parseable test output"}

def main() -> int:
    args = parse_args()
    args.pgid_file.write_text(str(os.getpgrp()), encoding="ascii")
    sys.path.insert(0, str(args.harness_checkout))
    module = importlib.import_module("swebench.harness.run_evaluation")
    original_create = module.create_container
    labels = dict(label.split("=", 1) for label in args.label)

    def labelled_create(test_spec, client, run_id, logger):
        if args.frozen_image:
            test_spec.image = args.frozen_image
        original_pull = client.images.pull
        def refuse_pull(*positional, **keywords):
            raise RuntimeError("frozen evaluator image is unavailable; pulling is forbidden")
        client.images.pull = refuse_pull
        original = client.containers.create
        def create(*positional, **keywords):
            merged = dict(keywords.pop("labels", {}) or {})
            merged.update(labels)
            return original(*positional, labels=merged, **keywords)
        client.containers.create = create
        try:
            return original_create(test_spec, client, run_id, logger)
        finally:
            client.containers.create = original
            client.images.pull = original_pull

    module.create_container = labelled_create
    if args.skip_patch:
        original_run_instances = module.run_instances
        def run_instances(*positional, **keywords):
            keywords["skip_patch"] = True
            return original_run_instances(*positional, **keywords)
        module.run_instances = run_instances
    args.report_dir.mkdir(parents=True, exist_ok=True)
    os.chdir(args.report_dir)
    import resource
    _, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
    safe_limit = 4096 if hard_limit == resource.RLIM_INFINITY else min(4096, hard_limit)
    module.main(dataset_name=str(args.dataset), split="test", instance_ids=[args.instance_id], predictions_path=args.predictions, max_workers=args.max_workers, open_file_limit=safe_limit, run_id=args.run_id, timeout=args.timeout, rewrite_reports=False, modal=False, report_dir=".", task_repo=None)
    log_root = Path.cwd() / "logs/run_evaluation" / args.run_id
    test_outputs = list(log_root.glob(f"*/{args.instance_id}/test_output.txt"))
    outcomes = None
    if test_outputs:
        from swebench.harness.grading import get_logs_eval
        from swebench.harness.utils import make_test_spec
        source = json.loads(args.dataset.read_text(encoding="utf-8"))[0]
        outcomes, found = get_logs_eval(make_test_spec(source), str(test_outputs[0]))
        if not found:
            outcomes = None
    if outcomes is None:
        result = classify_artifacts(log_root, args.instance_id, skip_patch=args.skip_patch)
    else:
        result = {"status": "completed", "classification": "test_results_parsed", "tests_executed": True, "outcomes": outcomes}
    (Path.cwd() / "adapter-result.json").write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    args.pgid_file.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
