from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Settings
from .data import load_prepared, prepare_data
from .errors import EvalError
from .validation import validate_benchmark


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evalsys", description="Frozen SWE-bench data checkpoint tools")
    parser.add_argument("--project-root", type=Path, default=Path.cwd(), help="repository root (default: current directory)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare-data", help="verify all source Git heads and generate frozen public/private manifests")
    sub.add_parser("validate-data", help="strictly validate existing manifests, hashes, membership, pairs, and distributions")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.from_env(args.project_root)
        if args.command == "prepare-data":
            prepared = prepare_data(settings)
            report = validate_benchmark(prepared)
        else:
            report = validate_benchmark(load_prepared(settings))
        print(json.dumps({"status": "passed", "command": args.command, **report}, ensure_ascii=False, sort_keys=True))
        return 0
    except EvalError as exc:
        print(f"ERROR [{exc.category}]: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
