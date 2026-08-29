from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .checkpoint import CheckpointStore
from .config import AgentConfig
from .context import ContextLedger
from .loop import AgentLoop
from .model import ModelMessage, ScriptedModel
from .tools import build_registry
from .trace import RunStore, atomic_json, atomic_text
from .workspace import GitWorkspace


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reqagent", description="Provider-neutral local Coding Agent")
    sub = parser.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor", help="validate an Agent configuration without network access")
    doctor.add_argument("--config", required=True, type=Path)
    doctor.add_argument("--live", action="store_true")
    run = sub.add_parser("run", help="run the Agent in an isolated copy of a clean Git repository")
    run.add_argument("--workspace", required=True, type=Path)
    task = run.add_mutually_exclusive_group(required=True)
    task.add_argument("--task")
    task.add_argument("--task-file", type=Path)
    run.add_argument("--config", required=True, type=Path)
    run.add_argument("--artifact-root", type=Path, default=project_root() / "artifacts/runs/iteration2/offline")
    resume = sub.add_parser("resume", help="resume an incomplete scripted run from its latest checkpoint")
    resume.add_argument("--run-id", required=True)
    resume.add_argument("--artifact-root", type=Path, default=project_root() / "artifacts/runs/iteration2/offline")
    resume.add_argument("--config", required=True, type=Path)
    return parser


def _prompts() -> tuple[str, str]:
    root = project_root() / "prompts/baseline"
    return root.joinpath("system.txt").read_text(encoding="utf-8"), root.joinpath("protocol.txt").read_text(encoding="utf-8")


def _execute(source: Path, task: str, config: AgentConfig, run_store: RunStore, *, destination: Path | None = None, position: int = 0, messages: list[dict] | None = None):
    workspace = GitWorkspace.create(source, destination=destination)
    system, protocol = _prompts()
    ledger = ContextLedger(system + "\n" + protocol, task, context_window=int(config.model["context_window_tokens"]), trigger_ratio=config.budgets["context_trigger_ratio"], keep_recent_rounds=config.budgets["keep_recent_rounds"])
    if messages is not None:
        ledger.messages = [ModelMessage.from_dict(message) for message in messages]
    model = ScriptedModel(config.script, position=position)
    registry = build_registry(workspace, config.raw)
    atomic_json(run_store.path / "run-manifest.json", {"run_id": run_store.run_id, "source": str(source), "workspace": str(workspace.root), "task": task, "base_commit": workspace.base_commit, "config_path": str(config.source)})
    atomic_json(run_store.path / "config.snapshot.json", config.public_dict())
    result = AgentLoop(model, registry, workspace, config, ledger, run_store).run()
    return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = AgentConfig.load(args.config)
        if args.command == "doctor":
            config.validate(live=args.live)
            print(json.dumps({"status": "passed", "mode": config.mode, "config_hash": config.canonical_hash()}, sort_keys=True))
            return 0
        if config.mode != "scripted":
            raise ValueError("only scripted mode is available at this implementation checkpoint")
        if args.command == "run":
            task = args.task if args.task is not None else args.task_file.read_text(encoding="utf-8")
            store = RunStore.create(args.artifact_root)
            result = _execute(args.workspace, task, config, store)
        else:
            store = RunStore.open(args.artifact_root / args.run_id)
            checkpoint = CheckpointStore(store.path).load()
            manifest = json.loads((store.path / "run-manifest.json").read_text(encoding="utf-8"))
            if checkpoint["config_hash"] != config.canonical_hash():
                raise ValueError("resume refused: configuration changed")
            if checkpoint["budgets"] != config.budgets:
                raise ValueError("resume refused: budgets changed")
            source = Path(manifest["source"])
            workspace_path = Path(manifest["workspace"])
            if not workspace_path.is_dir():
                raise ValueError("resume refused: workspace is missing")
            workspace = GitWorkspace(source, workspace_path, manifest["base_commit"])
            if workspace.diff_hash() != checkpoint["diff_hash"]:
                raise ValueError("resume refused: workspace changed")
            system, protocol = _prompts()
            ledger = ContextLedger(system + "\n" + protocol, manifest["task"], context_window=checkpoint["context_window"], trigger_ratio=config.budgets["context_trigger_ratio"], keep_recent_rounds=config.budgets["keep_recent_rounds"])
            ledger.messages = [ModelMessage.from_dict(message) for message in checkpoint["messages"]]
            model = ScriptedModel(config.script, position=checkpoint["script_position"] or 0)
            loop = AgentLoop(model, build_registry(workspace, config.raw), workspace, config, ledger, store)
            loop.steps = checkpoint["steps"]
            loop.tool_calls = checkpoint["tool_calls"]
            loop.invalid_outputs = checkpoint["invalid_outputs"]
            loop.usage = dict(checkpoint["usage"])
            result = loop.run()
        print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
