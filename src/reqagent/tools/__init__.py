from __future__ import annotations

from ..adaptive import AdaptiveRefinementState
from ..requirements import RequirementRefinementState
from ..workspace import GitWorkspace
from .adaptive import brief_definition, record_brief, reflect, reflection_definition
from .base import ToolRegistry
from .command import CommandExecutor, UnavailableCommandExecutor, definition as command_definition, run_command
from .files import list_definition, list_files, read_definition, read_file
from .patch import apply_patch, definition as patch_definition
from .requirements import definition as requirement_definition, record_requirement_baseline
from .search import definition as search_definition, search_text
from .submit import definition as submit_definition, submit


def build_registry(
    workspace: GitWorkspace,
    config: dict,
    *,
    command_executor: CommandExecutor | None = None,
    artifact_dir=None,
    requirement_refinement: bool | str = False,
    task: str = "",
) -> ToolRegistry:
    protected = tuple(config["workspace"]["protected_paths"])
    policy = workspace.policy(protected)
    refinement = RequirementRefinementState(task) if requirement_refinement is True else None
    adaptive = AdaptiveRefinementState(task) if requirement_refinement == "auto" else None
    registry = ToolRegistry(refinement=refinement)
    registry.adaptive = adaptive
    executor = command_executor or UnavailableCommandExecutor()
    command_artifacts = artifact_dir or workspace.root.parent / "commands"
    def with_evidence(name, handler):
        def execute(args):
            result = handler(args)
            if adaptive is not None and result.ok:
                evidence_id = adaptive.add_evidence(name, result.data)
                return type(result)(result.ok, result.tool, result.data, result.error, result.truncated, {**result.meta, "evidence_id": evidence_id})
            return result
        return execute
    registry.register(list_definition(), with_evidence("list_files", lambda args: list_files(policy, args)))
    registry.register(read_definition(), with_evidence("read_file", lambda args: read_file(policy, args)))
    registry.register(search_definition(), with_evidence("search_text", lambda args: search_text(policy, args)))
    if refinement is not None:
        registry.register(requirement_definition(), lambda args: record_requirement_baseline(refinement, args))
    if adaptive is not None and adaptive.route.mode == "refine":
        registry.register(brief_definition(), lambda args: record_brief(adaptive, args))
        registry.register(reflection_definition(), lambda args: reflect(adaptive, args))
    registry.register(patch_definition(), lambda args: apply_patch(workspace, protected, config["workspace"], args))
    registry.register(command_definition(), lambda args: run_command(workspace, protected, config["budgets"]["command_timeout_seconds"], executor, command_artifacts, len(registry.history) + 1, args))
    registry.register(submit_definition(), lambda args: submit(registry.history, args))
    return registry


__all__ = ["ToolRegistry", "build_registry"]
