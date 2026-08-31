from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from .checkpoint import CheckpointStore, canonical_hash, validate_resume_payload
from .config import AgentConfig
from .context import ContextLedger, ContextSummary
from .live import build_live_runtime
from .loop import AgentLoop
from .model import ModelMessage, ScriptedModel
from .tools import build_registry
from .trace import RunStore, atomic_json, atomic_text
from .workspace import GitWorkspace


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def run_kind(mode: str) -> str:
    return "live" if mode == "live" else "offline"


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


def _resume_identity(store: RunStore, config: AgentConfig, workspace: GitWorkspace, task: str, registry) -> dict[str, str]:
    root = project_root()
    package = root / "src/reqagent"
    system_path = root / "prompts/baseline/system.txt"
    protocol_path = root / "prompts/baseline/protocol.txt"
    protected = tuple(config.workspace["protected_paths"])
    return {
        "run_id": store.run_id,
        "source": str(workspace.source),
        "base_commit": workspace.base_commit,
        "code_hash": canonical_hash({path.relative_to(package).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(package.rglob("*.py"))}),
        "config_hash": config.canonical_hash(),
        "system_prompt_hash": hashlib.sha256(system_path.read_bytes()).hexdigest(),
        "protocol_prompt_hash": hashlib.sha256(protocol_path.read_bytes()).hexdigest(),
        "tool_schema_hash": canonical_hash([definition.__dict__ for definition in registry.definitions]),
        "task_hash": hashlib.sha256(task.encode("utf-8")).hexdigest(),
        "diff_hash": workspace.diff_hash(),
        "protected_fingerprint": workspace.protected_fingerprint(protected),
        "adapter_identity_hash": canonical_hash(getattr(registry, "adapter_identity", {"provider": config.mode})),
    }


def _execute(source: Path, task: str, config: AgentConfig, run_store: RunStore, *, destination: Path | None = None, position: int = 0, messages: list[dict] | None = None, finalize: bool = True):
    workspace = GitWorkspace.create(source, destination=destination)
    system, protocol = _prompts()
    ledger = ContextLedger(system + "\n" + protocol, task, context_window=int(config.model["context_window_tokens"]), trigger_ratio=config.budgets["context_trigger_ratio"], keep_recent_rounds=config.budgets["keep_recent_rounds"])
    if messages is not None:
        ledger.messages = [ModelMessage.from_dict(message) for message in messages]
    if config.mode == "live":
        model, executor = build_live_runtime(config, run_id=run_store.run_id)
    else:
        model, executor = ScriptedModel(config.script, position=position), None
    registry = build_registry(
        workspace,
        config.raw,
        command_executor=executor,
        artifact_dir=run_store.path / "commands",
        requirement_refinement=True,
        task=task,
    )
    registry.adapter_identity = getattr(model, "identity", {"provider": "scripted"})
    manifest_path = run_store.path / "run-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    manifest.update({"run_id": run_store.run_id, "source": str(source), "workspace": str(workspace.root), "task": task, "base_commit": workspace.base_commit, "config_path": str(config.source)})
    if config.mode == "live":
        manifest["adapter_identity"] = registry.adapter_identity
    atomic_json(manifest_path, manifest)
    snapshot_path = run_store.path / "config.snapshot.json"
    if snapshot_path.is_file():
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        if "run_id" in manifest:
            snapshot["agent"] = config.public_dict()
    else:
        snapshot = config.public_dict()
    atomic_json(snapshot_path, snapshot)
    result = AgentLoop(model, registry, workspace, config, ledger, run_store).run()
    if not finalize:
        (run_store.path / "result.json").replace(run_store.path / "agent-result.json")
        (run_store.path / "COMPLETE").replace(run_store.path / "AGENT_COMPLETE")
    if finalize and (run_store.path / "COMPLETE").is_file():
        workspace.cleanup()
    return result


def _resume_execute(config: AgentConfig, store: RunStore, *, finalize: bool = True):
    checkpoint = CheckpointStore(store.path).load()
    manifest = json.loads((store.path / "run-manifest.json").read_text(encoding="utf-8"))
    source = Path(manifest["source"])
    workspace_path = Path(manifest["workspace"])
    if not workspace_path.is_dir():
        raise ValueError("resume refused: workspace is missing")
    workspace = GitWorkspace(source, workspace_path, manifest["base_commit"])
    if config.mode == "live":
        model, executor = build_live_runtime(config, run_id=store.run_id)
    else:
        model, executor = ScriptedModel(config.script, position=checkpoint["adapter_position"] or 0), None
    registry = build_registry(
        workspace,
        config.raw,
        command_executor=executor,
        artifact_dir=store.path / "commands",
        requirement_refinement=True,
        task=manifest["task"],
    )
    registry.adapter_identity = getattr(model, "identity", {"provider": "scripted"})
    expected = _resume_identity(store, config, workspace, manifest["task"], registry)
    validate_resume_payload(checkpoint, expected, config.budgets)
    system, protocol = _prompts()
    ledger = ContextLedger(system + "\n" + protocol, manifest["task"], context_window=checkpoint["context_window"], trigger_ratio=config.budgets["context_trigger_ratio"], keep_recent_rounds=config.budgets["keep_recent_rounds"])
    ledger.messages = [ModelMessage.from_dict(message) for message in checkpoint["messages"]]
    ledger.summary = ContextSummary.from_dict(checkpoint["context_summary"])
    loop = AgentLoop(model, registry, workspace, config, ledger, store)
    loop.restore(checkpoint)
    result = loop.run()
    if not finalize:
        (store.path / "result.json").replace(store.path / "agent-result.json")
        (store.path / "COMPLETE").replace(store.path / "AGENT_COMPLETE")
    elif (store.path / "COMPLETE").is_file():
        workspace.cleanup()
    return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = AgentConfig.load(args.config)
        if args.command == "doctor":
            config.validate(live=args.live)
            report = {"status": "passed", "mode": config.mode, "config_hash": config.canonical_hash()}
            if args.live:
                adapter, executor = build_live_runtime(config, run_id="doctor")
                report.update({"adapter": adapter.identity, "container_image": executor.image})
            print(json.dumps(report, sort_keys=True))
            return 0
        if args.command == "run":
            task = args.task if args.task is not None else args.task_file.read_text(encoding="utf-8")
            store = RunStore.create(args.artifact_root, kind=run_kind(config.mode))
            result = _execute(args.workspace, task, config, store)
        else:
            store = RunStore.open(args.artifact_root / args.run_id)
            result = _resume_execute(config, store)
        print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
