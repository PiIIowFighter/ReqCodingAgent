from __future__ import annotations

from ..workspace import GitWorkspace
from .base import ToolRegistry
from .command import definition as command_definition, run_command
from .files import list_definition, list_files, read_definition, read_file
from .patch import apply_patch, definition as patch_definition
from .search import definition as search_definition, search_text
from .submit import definition as submit_definition, submit


def build_registry(workspace: GitWorkspace, config: dict) -> ToolRegistry:
    protected = tuple(config["workspace"]["protected_paths"])
    policy = workspace.policy(protected)
    registry = ToolRegistry()
    registry.register(list_definition(), lambda args: list_files(policy, args))
    registry.register(read_definition(), lambda args: read_file(policy, args))
    registry.register(search_definition(), lambda args: search_text(policy, args))
    registry.register(patch_definition(), lambda args: apply_patch(workspace, protected, args))
    registry.register(command_definition(), lambda args: run_command(workspace, config["budgets"]["command_timeout_seconds"], args))
    registry.register(submit_definition(), lambda args: submit(registry.history, args))
    return registry


__all__ = ["ToolRegistry", "build_registry"]
