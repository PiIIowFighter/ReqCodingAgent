"""Prepare a clean, reproducible stock-search demo project."""
from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = Path(__file__).resolve().parent / "fixtures" / "stock-search-template"


def prepare(target: Path) -> Path:
    target = target.expanduser().resolve()
    if target.exists():
        if not target.is_dir() or any(target.iterdir()):
            raise ValueError("target directory must be missing or empty")
    else:
        target.mkdir(parents=True)
    shutil.copytree(TEMPLATE, target, dirs_exist_ok=True)
    subprocess.run(["git", "init", "--quiet"], cwd=target, check=True)
    subprocess.run(["git", "-c", "user.email=reqagent-demo@local", "-c", "user.name=ReqCodingAgent Demo", "add", "."], cwd=target, check=True)
    subprocess.run(["git", "-c", "user.email=reqagent-demo@local", "-c", "user.name=ReqCodingAgent Demo", "commit", "--quiet", "-m", "baseline: stock board fixture"], cwd=target, check=True)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    try:
        print(prepare(args.target))
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
