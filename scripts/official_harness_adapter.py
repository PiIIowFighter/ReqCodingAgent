from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--harness-checkout", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--predictions", required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--timeout", type=int, required=True)
    parser.add_argument("--max-workers", type=int, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--skip-patch", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    sys.path.insert(0, str(args.harness_checkout))
    module = importlib.import_module("swebench.harness.run_evaluation")
    original_create = module.create_container

    def labelled_create(test_spec, client, run_id, logger):
        label_key, label_value = args.label.split("=", 1)
        original = client.containers.create
        def create(*positional, **keywords):
            labels = dict(keywords.pop("labels", {}) or {})
            labels[label_key] = label_value
            return original(*positional, labels=labels, **keywords)
        client.containers.create = create
        try:
            return original_create(test_spec, client, run_id, logger)
        finally:
            client.containers.create = original

    module.create_container = labelled_create
    if args.skip_patch:
        original_run_instances = module.run_instances
        def run_instances(*positional, **keywords):
            keywords["skip_patch"] = True
            return original_run_instances(*positional, **keywords)
        module.run_instances = run_instances
    args.report_dir.mkdir(parents=True, exist_ok=True)
    # Upstream writes logs/run_evaluation relative to cwd; keep all runtime logs
    # inside this case's artifact directory rather than the project checkout.
    import os
    os.chdir(args.report_dir)
    module.main(
        dataset_name=str(args.dataset), split="test", instance_ids=[args.instance_id], predictions_path=args.predictions,
        max_workers=args.max_workers, open_file_limit=4096, run_id=args.run_id, timeout=args.timeout,
        rewrite_reports=False, modal=False, report_dir=".", task_repo=None,
    )
    candidates = list(Path.cwd().glob(f"*.{args.run_id}.json"))
    report = json.loads(candidates[0].read_text()) if candidates else {}
    instance = report.get(args.instance_id, report)
    log_root = Path.cwd() / "logs/run_evaluation" / args.run_id
    reports = list(log_root.glob(f"*/{args.instance_id}/report.json"))
    detail = json.loads(reports[0].read_text()) if reports else {}
    detail_instance = detail.get(args.instance_id, detail)
    test_outputs = list(log_root.glob(f"*/{args.instance_id}/test_output.txt"))
    outcomes = None
    if test_outputs:
        from swebench.harness.grading import get_logs_eval
        from swebench.harness.utils import make_test_spec
        source = json.loads(args.dataset.read_text(encoding="utf-8"))[0]
        outcomes, found = get_logs_eval(make_test_spec(source), str(test_outputs[0]))
        if not found:
            outcomes = None
    result = {"tests_executed": bool(test_outputs and test_outputs[0].stat().st_size and outcomes is not None), "outcomes": outcomes, "official_report": instance}
    (Path.cwd() / "adapter-result.json").write_text(json.dumps(result, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
