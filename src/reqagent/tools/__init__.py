from __future__ import annotations

from ..workspace import GitWorkspace
from .base import ToolRegistry
from .command import CommandExecutor, UnavailableCommandExecutor, definition as command_definition, run_command
from .files import list_definition, list_files, read_definition, read_file
from .patch import apply_patch, definition as patch_definition
from .search import definition as search_definition, search_text
from .submit import definition as submit_definition, submit


def build_registry(
    workspace: GitWorkspace,
    config: dict,
    *,
    command_executor: CommandExecutor | None = None,
    artifact_dir=None,
) -> ToolRegistry:
    protected = tuple(config["workspace"]["protected_paths"])
    policy = workspace.policy(protected)
    registry = ToolRegistry()
    executor = command_executor or UnavailableCommandExecutor()
    command_artifacts = artifact_dir or workspace.root.parent / "commands"
    registry.register(list_definition(), lambda args: list_files(policy, args))
    registry.register(read_definition(), lambda args: read_file(policy, args))
    registry.register(search_definition(), lambda args: search_text(policy, args))
    registry.register(patch_definition(), lambda args: apply_patch(workspace, protected, config["workspace"], args))
    registry.register(command_definition(), lambda args: run_command(workspace, protected, config["budgets"]["command_timeout_seconds"], executor, command_artifacts, len(registry.history) + 1, args))
    registry.register(submit_definition(), lambda args: submit(registry.history, args))
    return registry


__all__ = ["ToolRegistry", "build_registry"]
