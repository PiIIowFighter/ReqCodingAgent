from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .checkpoint import CheckpointStore, canonical_hash
from .config import AgentConfig
from .context import ContextLedger
from .model import ModelAdapter, ModelMessage, ModelRequest, ModelResponse, NormalizedToolCall
from .patching import PatchResult, collect_patch, summarize_patch
from .tools.base import ToolRegistry
from .trace import RunStore, atomic_json, atomic_text
from .workspace import GitWorkspace, WorkspaceViolation


@dataclass(frozen=True)
class AgentResult:
    run_id: str
    stop_reason: str
    submitted: dict[str, Any] | None
    usage: dict[str, int]
    steps: int
    tool_calls: int
    patch: PatchResult
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "stop_reason": self.stop_reason, "submitted": self.submitted,
            "usage": self.usage, "steps": self.steps, "tool_calls": self.tool_calls,
            "patch": {"sha256": self.patch.sha256, "files": self.patch.files, "additions": self.patch.additions, "deletions": self.patch.deletions, "bytes": self.patch.bytes},
            "warnings": list(self.warnings),
        }


class AgentInterrupted(RuntimeError):
    pass


class AgentLoop:
    def __init__(self, adapter: ModelAdapter, registry: ToolRegistry, workspace: GitWorkspace, config: AgentConfig, context: ContextLedger, run_store: RunStore, *, interrupt_after: str | None = None):
        self.adapter = adapter
        self.registry = registry
        self.workspace = workspace
        self.config = config
        self.context = context
        self.run_store = run_store
        self.checkpoints = CheckpointStore(run_store.path)
        self.steps = 0
        self.tool_calls = 0
        self.invalid_outputs = 0
        self.usage: dict[str, int] = {}
        self.warnings: list[str] = []
        self.started = time.monotonic()
        self.elapsed_before_resume = 0.0
        self.submitted: dict[str, Any] | None = None
        self._repeat_fingerprint: str | None = None
        self._repeat_count = 0
        self.next_state = "call_model"
        self.pending_tool_calls: tuple[NormalizedToolCall, ...] = ()
        self.next_tool_index = 0
        self.interrupt_after = interrupt_after

    def _checkpoint(self, next_state: str) -> None:
        project = Path(__file__).resolve().parent
        system_path = project.parents[1] / "prompts/baseline/system.txt"
        protocol_path = project.parents[1] / "prompts/baseline/protocol.txt"
        protected = tuple(self.config.workspace["protected_paths"])
        self.checkpoints.save(self.steps + self.tool_calls + self.invalid_outputs, {
            "run_id": self.run_store.run_id,
            "source": str(self.workspace.source),
            "next_state": next_state,
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "invalid_outputs": self.invalid_outputs,
            "usage": self.usage,
            "messages": [message.to_dict() for message in self.context.messages],
            "context_window": self.context.context_window,
            "context_summary": self.context.summary.to_dict(),
            "config_hash": self.config.canonical_hash(),
            "code_hash": canonical_hash({path.relative_to(project).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(project.rglob("*.py"))}),
            "system_prompt_hash": hashlib.sha256(system_path.read_bytes()).hexdigest(),
            "protocol_prompt_hash": hashlib.sha256(protocol_path.read_bytes()).hexdigest(),
            "tool_schema_hash": canonical_hash([definition.__dict__ for definition in self.registry.definitions]),
            "base_commit": self.workspace.base_commit,
            "diff_hash": self.workspace.diff_hash(),
            "protected_fingerprint": self.workspace.protected_fingerprint(protected),
            "adapter_position": getattr(self.adapter, "position", None),
            "budgets": self.config.budgets,
            "task_hash": hashlib.sha256(self.context.task.encode("utf-8")).hexdigest(),
            "elapsed_seconds": self.elapsed_before_resume + time.monotonic() - self.started,
            "repeat_fingerprint": self._repeat_fingerprint,
            "repeat_count": self._repeat_count,
            "warnings": self.warnings,
            "tool_history": self.registry.history,
            "pending_tool_calls": [
                {"call_id": call.call_id, "name": call.name, "arguments": call.arguments}
                for call in self.pending_tool_calls
            ],
            "next_tool_index": self.next_tool_index,
        })

    def restore(self, checkpoint: dict[str, Any]) -> None:
        self.steps = checkpoint["steps"]
        self.tool_calls = checkpoint["tool_calls"]
        self.invalid_outputs = checkpoint["invalid_outputs"]
        self.usage = dict(checkpoint["usage"])
        self.elapsed_before_resume = float(checkpoint["elapsed_seconds"])
        self._repeat_fingerprint = checkpoint["repeat_fingerprint"]
        self._repeat_count = checkpoint["repeat_count"]
        self.warnings = list(checkpoint["warnings"])
        self.registry.history.extend(checkpoint["tool_history"])
        self.next_state = checkpoint["next_state"]
        self.pending_tool_calls = tuple(NormalizedToolCall(**call) for call in checkpoint["pending_tool_calls"])
        self.next_tool_index = checkpoint["next_tool_index"]

    def _interrupt(self, point: str) -> None:
        if self.interrupt_after == point:
            raise AgentInterrupted(point)

    def _call_model(self) -> ModelResponse:
        request = ModelRequest(tuple(self.context.messages), self.registry.definitions, int(self.config.model["max_output_tokens"]), self.config.budgets["model_timeout_seconds"])
        attempts = 0
        while True:
            try:
                return self.adapter.complete(request)
            except Exception as exc:
                retryable = bool(getattr(exc, "retryable", False))
                if not retryable or attempts >= self.config.budgets["max_retries"]:
                    raise
                attempts += 1
                self.run_store.event("model_retry", attempt=attempts, category=getattr(exc, "category", "unknown"))
                time.sleep(min(0.1 * 2 ** (attempts - 1), 1.0))

    def run(self) -> AgentResult:
        stop_reason = "internal_error"
        try:
            while True:
                if self.elapsed_before_resume + time.monotonic() - self.started >= self.config.budgets["wall_clock_seconds"]:
                    stop_reason = "wall_clock_timeout"
                    break
                if self.next_state == "call_model":
                    if self.steps >= self.config.budgets["max_steps"]:
                        stop_reason = "step_budget"
                        break
                    self.steps += 1
                    response = self._call_model()
                    self.run_store.event("model_response", sequence=self.steps, response=response.to_dict())
                    for key, value in response.usage.items():
                        self.usage[key] = self.usage.get(key, 0) + value
                    self.context.add(ModelMessage("assistant", response.text, response.tool_calls))
                    if response.finish_reason == "refusal":
                        stop_reason = "model_refusal"
                        break
                    if not response.tool_calls:
                        self.invalid_outputs += 1
                        self.pending_tool_calls = ()
                        self.next_tool_index = 0
                        self.next_state = "call_model"
                        self._checkpoint(self.next_state)
                        if self.invalid_outputs >= self.config.budgets["max_invalid_outputs"]:
                            stop_reason = "invalid_output_limit"
                            break
                        continue
                    self.pending_tool_calls = response.tool_calls
                    self.next_tool_index = 0
                    self.next_state = "execute"
                    self._checkpoint(self.next_state)
                    self._interrupt("after_model_checkpoint")

                while self.next_state == "execute":
                    if self.next_tool_index >= len(self.pending_tool_calls):
                        raise ValueError("execute state has no pending tool call")
                    if self.tool_calls >= self.config.budgets["max_tool_calls"]:
                        stop_reason = "tool_budget"
                        break
                    call = self.pending_tool_calls[self.next_tool_index]
                    before = self.workspace.diff_hash()
                    fingerprint = hashlib.sha256(json.dumps({"name": call.name, "arguments": call.arguments, "diff": before}, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
                    result = self.registry.execute(call.name, call.arguments)
                    self.tool_calls += 1
                    if not result.ok and result.error and result.error["kind"] in {"invalid_arguments", "unknown_tool"}:
                        self.invalid_outputs += 1
                    after = self.workspace.diff_hash()
                    stable_result = {"ok": result.ok, "data": result.data, "error": result.error, "truncated": result.truncated}
                    outcome = hashlib.sha256(json.dumps(stable_result, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
                    repeat = fingerprint + outcome + after
                    if repeat == self._repeat_fingerprint and after == before:
                        self._repeat_count += 1
                        if self._repeat_count == 1:
                            self.warnings.append("repeated action made no progress")
                        elif self._repeat_count >= 2:
                            stop_reason = "repeated_action"
                    else:
                        self._repeat_fingerprint = repeat
                        self._repeat_count = 0
                    self.context.add(ModelMessage("tool", tool_results=({"call_id": call.call_id, **result.to_dict()},)))
                    self.run_store.event("tool_result", sequence=self.tool_calls, call_id=call.call_id, result=result.to_dict())
                    self.context.compact_if_needed(self.registry.history, after)
                    self.next_tool_index += 1
                    if call.name == "submit" and result.ok:
                        self.submitted = result.data
                        self.pending_tool_calls = ()
                        self.next_tool_index = 0
                        self.next_state = "call_model"
                        self._checkpoint(self.next_state)
                        stop_reason = "submitted"
                        break
                    if self.next_tool_index < len(self.pending_tool_calls):
                        self.next_state = "execute"
                    else:
                        self.pending_tool_calls = ()
                        self.next_tool_index = 0
                        self.next_state = "call_model"
                    self._checkpoint(self.next_state)
                    self._interrupt("after_tool_checkpoint")
                    if self.invalid_outputs >= self.config.budgets["max_invalid_outputs"]:
                        stop_reason = "invalid_output_limit"
                        break
                    if stop_reason == "repeated_action":
                        break
                if stop_reason in {"submitted", "tool_budget", "repeated_action", "invalid_output_limit"}:
                    break
        except AgentInterrupted:
            raise
        except WorkspaceViolation:
            stop_reason = "workspace_violation"
        except Exception as exc:
            stop_reason = "unrecoverable_model_error" if hasattr(exc, "category") else "internal_error"
            self.warnings.append(str(exc))
        return self._finish(stop_reason)

    def _finish(self, stop_reason: str) -> AgentResult:
        try:
            patch = collect_patch(self.workspace, self.config.workspace, protected_paths=tuple(self.config.workspace["protected_paths"]))
        except WorkspaceViolation as exc:
            patch = summarize_patch(self.workspace.diff())
            stop_reason = "patch_limit" if "limit" in str(exc) else "workspace_violation"
            self.warnings.append(str(exc))
        atomic_text(self.run_store.path / "agent.patch", patch.text)
        result = AgentResult(self.run_store.run_id, stop_reason, self.submitted, self.usage, self.steps, self.tool_calls, patch, tuple(self.warnings))
        atomic_json(self.run_store.path / "result.json", result.to_dict())
        atomic_text(self.run_store.path / "COMPLETE", "complete\n")
        return result
