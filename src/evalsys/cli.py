from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .acceptance import evaluate_acceptance
from .config import Settings
from .data import load_prepared, prepare_data
from .errors import EvalError
from .frozen_cases import CASE_IDS
from .isolation import prove_isolation
from .preflight import run_preflight
from .reporting import generate_report, generate_smoke_report, publish_audit
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
    report = sub.add_parser("report", help="aggregate iteration-1 evidence or a frozen iteration-2 baseline")
    report_target = report.add_mutually_exclusive_group(required=True)
    report_target.add_argument("run_directory", type=Path, nargs="?")
    report_target.add_argument("--name", help="frozen iteration-2 baseline name")
    smoke = sub.add_parser("smoke-report", help="aggregate one django__django-11133 noop/gold pair")
    smoke.add_argument("--noop-run", type=Path, required=True)
    smoke.add_argument("--gold-run", type=Path, required=True)
    validate_all = sub.add_parser("validate-all", help="run and resume the complete iteration-1 validation pipeline")
    validate_all.add_argument("--timeout", type=int, default=1800)
    validate_all.add_argument("--workers", type=int, default=1)
    validate_all.add_argument("--resume", action="store_true")
    validate_all.add_argument("--run-id")
    validate_all.add_argument("--noop-run-id")
    validate_all.add_argument("--gold-run-id")
    validate_all.add_argument("--task-repo", type=Path)
    validate_all.add_argument("--isolation-workspace", type=Path)
    agent_run = sub.add_parser("agent-run", help="run one protected Agent benchmark cell")
    agent_run.add_argument("--case-id", required=True)
    agent_run.add_argument("--variant", choices=("full", "fuzzy"), required=True)
    agent_run.add_argument("--config", type=Path, required=True)
    agent_run.add_argument("--confirm", action="store_true")
    agent_run.add_argument("--resume", action="store_true")
    agent_run.add_argument("--run-id")
    run_dev = sub.add_parser("run-dev", help="run the protected development matrix")
    run_dev.add_argument("--version", required=True)
    run_dev.add_argument("--config", type=Path, required=True)
    run_dev.add_argument("--confirm", action="store_true")
    run_dev.add_argument("--resume", action="store_true")
    freeze = sub.add_parser("freeze-baseline", help="freeze an accepted development baseline")
    freeze.add_argument("--name", required=True)
    freeze.add_argument("--dev-version", required=True)
    freeze.add_argument("--config", type=Path, required=True)
    freeze.add_argument("--confirm", action="store_true")
    formal = sub.add_parser("run-formal", help="protected formal evaluation entry point")
    formal.add_argument("--name", required=True)
    formal.add_argument("--confirm", action="store_true")
    formal.add_argument("--resume", action="store_true")
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
            if args.name:
                from .iteration2 import load_formal_results, summarize_formal_results, verify_frozen_baseline
                frozen = verify_frozen_baseline(settings.project_root / "configs/frozen" / args.name, project_root=settings.project_root)
                formal_root = settings.artifact_root / "runs/iteration2/formal" / args.name
                rows = load_formal_results(settings.project_root, settings.artifact_root / "runs/iteration2", frozen["plan"], formal_root / "experiment-manifest.json")
                report = summarize_formal_results(rows)
                destination = settings.project_root / "audit/iteration2/reports" / f"{args.name}.json"
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                report = {"status": "passed", "report": str(destination), **report}
            else:
                prepared = load_prepared(settings)
                instance_ids = list(CASE_IDS)
                paths = generate_report(args.run_directory, expected_instance_ids=instance_ids)
                report = {"status": json.loads(paths.machine_json.read_text(encoding="utf-8"))["status"], "run_directory": str(args.run_directory), "machine_json": str(paths.machine_json), "machine_jsonl": str(paths.machine_jsonl), "markdown": str(paths.markdown)}
        elif args.command == "smoke-report":
            validation_receipt = validate_benchmark(load_prepared(settings))["validation_receipt"]
            paths = generate_smoke_report(
                args.noop_run,
                args.gold_run,
                destination=settings.project_root / "audit/iteration1",
                validation_receipt=validation_receipt,
            )
            report = {"status": json.loads(paths.machine_json.read_text(encoding="utf-8"))["status"], "machine_json": str(paths.machine_json), "markdown": str(paths.markdown)}
        elif args.command in {"agent-run", "run-dev", "freeze-baseline", "run-formal"}:
            from reqagent.config import AgentConfig
            from .harness_environment import verify_settings_harness_environment
            from .agent_runner import preflight_agent_config
            from .baseline import require_frozen_baseline
            from .iteration2 import (
                behavior_tree_hash, current_tool_schema_bytes, development_cells, freeze_baseline,
                load_provider_identity, load_public_records, run_agent_cell, run_development,
                run_formal_plan, select_public_case, verify_frozen_baseline, verify_git_gate,
            )
            if not args.confirm:
                raise EvalError(f"{args.command} requires --confirm", category="invalid")
            if args.command in {"agent-run", "run-dev", "freeze-baseline"}:
                config = preflight_agent_config(args.config, confirmed=True)
            harness_environment = verify_settings_harness_environment(settings)
            prepared = load_prepared(settings)
            source_rows = {
                instance_id: {**row, "harness_revision": prepared.lock_heads["harness"]}
                for instance_id, row in prepared.source_rows.items()
            }
            records = load_public_records(settings.project_root)
            provider_identity = load_provider_identity(
                settings.project_root,
                "audit/iteration2/runs/20260829T130000000000Z-live-capability/summary.json",
            )
            if args.command == "agent-run":
                if args.resume != bool(args.run_id):
                    raise EvalError("--resume and --run-id must be supplied together", category="invalid")
                if args.resume:
                    raise EvalError("single-cell resume is managed by run-dev/run-formal", category="invalid")
                case = select_public_case(records, args.case_id, args.variant, allowed_split="dev")
                inventory_path = settings.project_root / "audit/iteration2/image-inventory.json"
                inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
                report = run_agent_cell(settings, case, source_rows[case["instance_id"]], config, image_identity=inventory[case["instance_id"]], run_type="manual_cell", provider_identity=provider_identity)
                report = {"status": "passed", "cell": report}
            elif args.command == "run-dev":
                from .recovery import sha256_file
                inventory_path = settings.project_root / "audit/iteration2/image-inventory.json"
                inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
                def gate_reference(name: str) -> dict[str, str]:
                    path = settings.project_root / "audit/iteration2" / name
                    if not path.is_file():
                        raise EvalError(f"iteration2 gate evidence is missing: {name}", category="invalid")
                    return {"path": path.relative_to(settings.project_root).as_posix(), "sha256": sha256_file(path)}
                report = {"status": "passed", "development": run_development(
                    settings, args.version, config, source_rows, inventory, resume=args.resume,
                    test_receipt=gate_reference("test-receipt.json"),
                    isolation_proof=gate_reference("isolation-proof.json"),
                    provider_identity=provider_identity,
                    harness_environment=harness_environment["reference"],
                )}
            elif args.command == "freeze-baseline":
                commit = verify_git_gate(settings.project_root)
                development_path = settings.project_root / "audit/iteration2/development" / f"{args.dev_version}.json"
                if not development_path.is_file():
                    raise EvalError("development record is missing", category="invalid")
                development = json.loads(development_path.read_text(encoding="utf-8"))
                inventory_path = settings.project_root / "audit/iteration2/image-inventory.json"
                if not inventory_path.is_file():
                    raise EvalError("task image inventory is missing", category="invalid")
                images = json.loads(inventory_path.read_text(encoding="utf-8"))
                destination = freeze_baseline(settings.project_root, args.name, config.raw, records, development=development, image_identities=images, authorized=True, git_commit=commit)
                report = {"status": "passed", "baseline": str(destination)}
            else:
                baseline_root = require_frozen_baseline(settings.project_root, args.name)
                frozen = verify_frozen_baseline(baseline_root)
                if frozen["baseline"].get("harness_environment") != harness_environment.get("reference"):
                    raise EvalError("current harness environment differs from frozen receipt", category="infra_failed")
                verify_git_gate(settings.project_root)
                frozen_config = AgentConfig(frozen["baseline"]["config"], baseline_root / "baseline.json")
                frozen_config.validate(live=True)
                report = {"status": "passed", **run_formal_plan(settings, args.name, frozen_config, source_rows, resume=args.resume)}
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
                if name == "unit_tests":
                    completed = subprocess.run([sys.executable, "-m", "pytest", "-q", "-m", "not integration"], cwd=settings.project_root, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, env={**dict(__import__("os").environ), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"})
                    summary = completed.stdout[-4000:]
                    return {"status": "passed" if completed.returncode == 0 else "failed", "failure_kind": "infra", "exit_code": completed.returncode, "summary": summary}
                records = validate_jsonl(prepared.public_manifest, "public-case")
                cases = [record for record in records if record["prompt_variant"] == "full"]
                if name == "isolation":
                    if args.task_repo is None or args.isolation_workspace is None:
                        raise EvalError("validate-all isolation requires --task-repo and --isolation-workspace", hint="Pass a clean external task repository and fresh workspace", category="infra_failed")
                    proof = prove_isolation(settings.project_root, args.task_repo, cases[0], args.isolation_workspace, docker_prefix=settings.docker_prefix(sys.platform))
                    return {"status": "passed", "proof": proof}
                if name in {"replay_noop", "replay_gold"}:
                    mode = name.removeprefix("replay_")
                    override = args.noop_run_id if mode == "noop" else args.gold_run_id
                    prior = context.get("resume_child")
                    explicit = override or (prior["run_id"] if prior else None)
                    child_resume = bool(explicit and args.resume)
                    child = replay_cases(settings, cases, prepared.source_rows, mode, harness_revision=prepared.lock_heads["harness"], timeout_s=args.timeout, workers=args.workers, resume=child_resume, run_id=explicit)
                    failure_kind = "infra" if any(row["status"] in {"infra_failed", "timeout", "invalid"} for row in child["results"]) else "test"
                    return {"status": child["status"], "failure_kind": failure_kind, "run_id": child["run_id"], "run_directory": child["run_directory"]}
                if name == "aggregation":
                    paths = generate_report(context["run_directory"], expected_instance_ids=sorted({case["instance_id"] for case in cases}))
                    return {"status": json.loads(paths.machine_json.read_text(encoding="utf-8"))["status"]}
                if name == "audit":
                    tests = context["state"]["stages"].get("unit_tests", {})
                    publish_audit(context["run_directory"], settings.project_root / "audit/iteration1/runs" / context["run_id"], test_summary=tests.get("summary", f"pytest exit_code={tests.get('exit_code', 'unknown')}"))
                    return {"status": "passed"}
                machine = json.loads((context["run_directory"] / "validation-summary.json").read_text(encoding="utf-8"))
                strict = context["state"]["stages"].get("strict_data", {})
                machine["validation_receipt"] = strict.get("validation_receipt")
                machine["resume_verified"] = context["state"].get("resume_verified") is True
                return {"status": machine["status"], "optional_full_profile": True, "iteration_completion": False, "reason": "The optional 15x2 profile does not replace the iteration-1 smoke acceptance gate"}
            report = run_validate_all(settings.project_root, stage_runner=stage_runner, run_id=args.run_id, resume=args.resume, config=config, artifact_root=settings.artifact_root)
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
        status = report.get("status", "passed")
        print(json.dumps({"status": status, "command": args.command, **report}, ensure_ascii=True, sort_keys=True))
        return 0 if status == "passed" else 1
    except (EvalError, ValueError) as exc:
        category = exc.category if isinstance(exc, EvalError) else "invalid"
        print(f"ERROR [{category}]: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
