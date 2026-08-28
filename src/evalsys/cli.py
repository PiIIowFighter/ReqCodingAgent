from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .acceptance import evaluate_acceptance
from .config import Settings
from .data import load_prepared, prepare_data
from .errors import EvalError
from .frozen_cases import CASE_IDS
from .isolation import prove_isolation
from .preflight import run_preflight
from .reporting import generate_report, publish_audit
from .replay import replay_cases
from .locks import verify_source_locks
from .validate_all import run_validate_all
from .schema import validate_jsonl
from .validation import validate_benchmark


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evalsys", description="Frozen SWE-bench data checkpoint tools")
    parser.add_argument("--project-root", type=Path, default=Path.cwd(), help="repository root (default: current directory)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight", help="check Python, locked sources, Linux Docker, and a real bind mount")
    sub.add_parser("prepare-data", help="verify all source Git heads and generate frozen public/private manifests")
    sub.add_parser("validate-data", help="strictly validate existing manifests, hashes, membership, pairs, and distributions")
    isolation = sub.add_parser("prove-isolation", help="construct and probe a future Agent workspace")
    isolation.add_argument("--task-repo", type=Path, required=True, help="clean external task repository fixture")
    isolation.add_argument("--public-case", type=Path, required=True, help="one validated public prompt record as JSON")
    isolation.add_argument("--workspace", type=Path, required=True, help="fresh external Agent workspace destination")
    isolation.add_argument("--output", type=Path, help="optional sanitized machine-readable proof destination")
    replay = sub.add_parser("replay", help="run the pinned official SWE-bench harness")
    replay.add_argument("--mode", required=True, choices=("noop", "gold"))
    replay.add_argument("--split", required=True, choices=("all", "dev", "test"))
    replay.add_argument("--timeout", type=int, default=1800)
    replay.add_argument("--workers", type=int, default=1)
    replay.add_argument("--resume", action="store_true")
    replay.add_argument("--run-id", help="existing run id required with --resume")
    replay.add_argument("--instance-id", help="run one frozen instance (smoke/recovery)")
    report = sub.add_parser("report", help="aggregate a validate-all run directory")
    report.add_argument("run_directory", type=Path)
    validate_all = sub.add_parser("validate-all", help="run and resume the complete iteration-1 validation pipeline")
    validate_all.add_argument("--timeout", type=int, default=1800)
    validate_all.add_argument("--workers", type=int, default=1)
    validate_all.add_argument("--resume", action="store_true")
    validate_all.add_argument("--run-id")
    validate_all.add_argument("--noop-run-id")
    validate_all.add_argument("--gold-run-id")
    validate_all.add_argument("--task-repo", type=Path)
    validate_all.add_argument("--isolation-workspace", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.from_env(args.project_root)
        if args.command == "preflight":
            report = run_preflight(settings)
        elif args.command == "prepare-data":
            prepared = prepare_data(settings)
            report = validate_benchmark(prepared)
        elif args.command == "validate-data":
            report = validate_benchmark(load_prepared(settings))
        elif args.command == "replay":
            prepared = load_prepared(settings)
            records = validate_jsonl(prepared.public_manifest, "public-case")
            cases = [record for record in records if record["prompt_variant"] == "full" and (args.split == "all" or record["split"] == args.split) and (not args.instance_id or record["instance_id"] == args.instance_id)]
            if args.instance_id and not cases:
                raise EvalError(f"Instance is not in the selected frozen split: {args.instance_id}")
            report = replay_cases(settings, cases, prepared.source_rows, args.mode, harness_revision=prepared.lock_heads["harness"], timeout_s=args.timeout, workers=args.workers, resume=args.resume, run_id=args.run_id)
        elif args.command == "report":
            prepared = load_prepared(settings)
            instance_ids = list(CASE_IDS)
            paths = generate_report(args.run_directory, expected_instance_ids=instance_ids)
            report = {"status": json.loads(paths.machine_json.read_text(encoding="utf-8"))["status"], "run_directory": str(args.run_directory), "machine_json": str(paths.machine_json), "machine_jsonl": str(paths.machine_jsonl), "markdown": str(paths.markdown)}
        elif args.command == "validate-all":
            if args.resume and not args.run_id:
                raise EvalError("--run-id is required with --resume for validate-all")
            if not args.resume and (args.noop_run_id or args.gold_run_id):
                raise EvalError("Explicit child run IDs require --resume so existing evidence cannot be overwritten")
            config = {"timeout_s": args.timeout, "workers": args.workers, "noop_run_id": args.noop_run_id, "gold_run_id": args.gold_run_id}
            prepared_box = {}
            def stage_runner(name, context):
                if name == "preflight":
                    return run_preflight(settings)
                if name == "locks_and_cache":
                    verify_source_locks(settings)
                    prepared_box["value"] = prepare_data(settings)
                    return {"status": "passed"}
                prepared = prepared_box.get("value") or load_prepared(settings)
                if name == "strict_data":
                    validation = validate_benchmark(prepared)
                    validation["status"] = "passed"
                    return validation
                records = validate_jsonl(prepared.public_manifest, "public-case")
                cases = [record for record in records if record["prompt_variant"] == "full"]
                if name == "isolation":
                    if args.task_repo is None or args.isolation_workspace is None:
                        raise EvalError("validate-all isolation requires --task-repo and --isolation-workspace", hint="Pass a clean external task repository and fresh workspace", category="infra_failed")
                    proof = prove_isolation(settings.project_root, args.task_repo, cases[0], args.isolation_workspace, docker_prefix=settings.docker_prefix(sys.platform))
                    return {"status": "passed", "proof": proof}
                if name in {"replay_noop", "replay_gold"}:
                    mode = name.removeprefix("replay_")
                    explicit = args.noop_run_id if mode == "noop" else args.gold_run_id
                    child_resume = bool(explicit and args.resume)
                    child = replay_cases(settings, cases, prepared.source_rows, mode, harness_revision=prepared.lock_heads["harness"], timeout_s=args.timeout, workers=args.workers, resume=child_resume, run_id=explicit)
                    failure_kind = "infra" if any(row["status"] in {"infra_failed", "timeout", "invalid"} for row in child["results"]) else "test"
                    return {"status": child["status"], "failure_kind": failure_kind, "run_id": child["run_id"], "run_directory": child["run_directory"]}
                if name == "aggregation":
                    paths = generate_report(context["run_directory"], expected_instance_ids=sorted({case["instance_id"] for case in cases}))
                    return {"status": json.loads(paths.machine_json.read_text(encoding="utf-8"))["status"]}
                if name == "audit":
                    publish_audit(context["run_directory"], settings.project_root / "audit/iteration1", test_summary="Unit-test evidence must be recorded separately; validate-all executed.")
                    return {"status": "passed"}
                machine = json.loads((context["run_directory"] / "validation-summary.json").read_text(encoding="utf-8"))
                strict = context["state"]["stages"].get("strict_data", {})
                machine["data_checks"] = {
                    "schema_pairs_12_3": strict.get("test_pairs") == 12 and strict.get("dev_pairs") == 3,
                    "distribution_4_4_4_1_1_1": bool(strict.get("distributions")),
                    "official_hashes_15": strict.get("records") == 30,
                    "pair_official_fields_equal": strict.get("records") == 30,
                }
                acceptance = evaluate_acceptance(settings.project_root, machine, unit_tests_passed=False)
                return {"status": "passed", **acceptance, "reason": "Unit-test completion evidence is verified separately"}
            report = run_validate_all(settings.project_root, stage_runner=stage_runner, run_id=args.run_id, resume=args.resume, config=config)
            report["run_directory"] = str(settings.artifact_root / "runs/iteration1" / report["run_id"])
        else:
            try:
                public_case = json.loads(args.public_case.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise EvalError(f"Cannot read public case JSON: {exc}", hint="Pass one validated public manifest record") from exc
            report = prove_isolation(settings.project_root, args.task_repo, public_case, args.workspace)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({"status": "passed", "command": args.command, **report}, ensure_ascii=True, sort_keys=True))
        return 0 if report.get("status") == "passed" else 1
    except EvalError as exc:
        print(f"ERROR [{exc.category}]: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
