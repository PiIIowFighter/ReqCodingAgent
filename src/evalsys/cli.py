from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Settings
from .data import load_prepared, prepare_data
from .errors import EvalError
from .isolation import prove_isolation
from .preflight import run_preflight
from .replay import replay_cases
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
            cases = [record for record in records if record["prompt_variant"] == "full" and (args.split == "all" or record["split"] == args.split)]
            report = replay_cases(settings, cases, prepared.source_rows, args.mode, timeout_s=args.timeout, workers=args.workers, resume=args.resume)
        else:
            try:
                public_case = json.loads(args.public_case.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise EvalError(f"Cannot read public case JSON: {exc}", hint="Pass one validated public manifest record") from exc
            report = prove_isolation(settings.project_root, args.task_repo, public_case, args.workspace)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({"status": "passed", "command": args.command, **report}, ensure_ascii=False, sort_keys=True))
        return 0
    except EvalError as exc:
        print(f"ERROR [{exc.category}]: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
