from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from evalsys.evidence import EvidenceRecorder


def decode_output(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")


def emit_output(buffer, value: bytes) -> None:
    buffer.write(value)
    buffer.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a command and preserve iteration evidence")
    parser.add_argument("--type", required=True, choices=("unit_tests", "preflight", "validate_data", "replay_noop", "replay_gold", "validate_all"))
    parser.add_argument("--config", default="{}", help="JSON object containing non-secret run configuration")
    parser.add_argument("--supersedes", action="append", default=[], help="Prior run_id explicitly superseded by this run")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required after --")
    config = json.loads(args.config)
    if not isinstance(config, dict):
        parser.error("--config must decode to an object")
    root = Path(__file__).resolve().parents[1]
    run = EvidenceRecorder(root, iteration=1).start(args.type, config, command, supersedes=args.supersedes)
    run.record_event("command_started", {"argv": command})
    completed = subprocess.run(command, cwd=root, capture_output=True, check=False)
    stdout = decode_output(completed.stdout)
    stderr = decode_output(completed.stderr)
    import re
    combined = stdout + "\n" + stderr
    passed_match = re.search(r"(\d+) passed", combined)
    failed_match = re.search(r"(\d+) failed", combined)
    skipped_match = re.search(r"(\d+) skipped", combined)
    duration_match = re.search(r"in ([0-9.]+)s", combined)
    result = {
        "status": "passed" if completed.returncode == 0 else "failed",
        "passed": int(passed_match.group(1)) if passed_match else 0,
        "failed": int(failed_match.group(1)) if failed_match else 0,
        "skipped": int(skipped_match.group(1)) if skipped_match else 0,
        "duration": float(duration_match.group(1)) if duration_match else None,
        "exit_code": completed.returncode,
    }
    run.record_event("command_finished", result)
    if completed.returncode == 0:
        run.finish(result, stdout=stdout, stderr=stderr)
    else:
        run.fail(result, stdout=stdout, stderr=stderr)
    print(run.run_id, flush=True)
    emit_output(sys.stdout.buffer, completed.stdout)
    emit_output(sys.stderr.buffer, completed.stderr)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
