from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from evalsys.evidence import EvidenceRecorder


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a command and preserve iteration evidence")
    parser.add_argument("--type", required=True, choices=("unit_tests", "preflight", "validate_data", "replay_noop", "replay_gold", "validate_all"))
    parser.add_argument("--config", default="{}", help="JSON object containing non-secret run configuration")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required after --")
    config = json.loads(args.config)
    if not isinstance(config, dict):
        parser.error("--config must decode to an object")
    root = Path(__file__).resolve().parents[1]
    run = EvidenceRecorder(root, iteration=1).start(args.type, config, command)
    run.record_event("command_started", {"argv": command})
    completed = subprocess.run(command, cwd=root, text=True, capture_output=True, check=False)
    result = {"status": "passed" if completed.returncode == 0 else "failed", "exit_code": completed.returncode}
    run.record_event("command_finished", result)
    if completed.returncode == 0:
        run.finish(result, stdout=completed.stdout, stderr=completed.stderr)
    else:
        run.fail(result, stdout=completed.stdout, stderr=completed.stderr)
    print(run.run_id)
    sys.stdout.write(completed.stdout)
    sys.stderr.write(completed.stderr)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
