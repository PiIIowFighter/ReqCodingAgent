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
        self.consecutive_invalid_outputs = 0
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
            "consecutive_invalid_outputs": self.consecutive_invalid_outputs,
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
            "adapter_identity_hash": canonical_hash(getattr(self.adapter, "identity", {"provider": "scripted"})),
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
            "requirement_refinement": self.registry.refinement.to_checkpoint() if self.registry.refinement is not None else None,
            "adaptive_refinement": self.registry.adaptive.to_checkpoint() if getattr(self.registry, "adaptive", None) is not None else None,
        })

    def restore(self, checkpoint: dict[str, Any]) -> None:
        self.steps = checkpoint["steps"]
        self.tool_calls = checkpoint["tool_calls"]
        self.invalid_outputs = checkpoint["invalid_outputs"]
        self.consecutive_invalid_outputs = checkpoint.get("consecutive_invalid_outputs", checkpoint["invalid_outputs"])
        self.usage = dict(checkpoint["usage"])
        self.elapsed_before_resume = float(checkpoint["elapsed_seconds"])
        self._repeat_fingerprint = checkpoint["repeat_fingerprint"]
        self._repeat_count = checkpoint["repeat_count"]
        self.warnings = list(checkpoint["warnings"])
        self.registry.history.extend(checkpoint["tool_history"])
        self.next_state = checkpoint["next_state"]
        self.pending_tool_calls = tuple(NormalizedToolCall(**call) for call in checkpoint["pending_tool_calls"])
        self.next_tool_index = checkpoint["next_tool_index"]
        refinement = checkpoint["requirement_refinement"]
        if (self.registry.refinement is None) != (refinement is None):
            raise ValueError("resume refused: requirement refinement mode changed")
        if self.registry.refinement is not None:
            self.registry.refinement.restore(refinement)
        adaptive = checkpoint["adaptive_refinement"]
        current_adaptive = getattr(self.registry, "adaptive", None)
        if (current_adaptive is None) != (adaptive is None):
            raise ValueError("resume refused: adaptive refinement mode changed")
        if current_adaptive is not None:
            current_adaptive.restore(adaptive)

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

    def _write_static_evidence(self) -> None:
        atomic_text(self.run_store.path / "prompt.snapshot.txt", self.context.messages[0].text + "\n\n" + self.context.task + "\n")
        atomic_json(self.run_store.path / "tool-schemas.json", [definition.__dict__ for definition in self.registry.schema_definitions])
        atomic_json(self.run_store.path / "workspace-before.json", {"base_commit": self.workspace.base_commit, "diff_sha256": hashlib.sha256(b"").hexdigest()})
        adaptive = getattr(self.registry, "adaptive", None)
        if adaptive is not None and adaptive.route.mode == "refine" and not any(message.text.startswith("Adaptive refinement phase:") for message in self.context.messages):
            self.context.add(ModelMessage("system", adaptive.refinement_instruction()))

    def run(self) -> AgentResult:
        self._write_static_evidence()
        stop_reason = "internal_error"
        try:
            while True:
                if self.elapsed_before_resume + time.monotonic() - self.started >= self.config.budgets["wall_clock_seconds"]:
                    stop_reason = "wall_clock_timeout"
                    break
                if self.next_state == "call_model":
                    adaptive = getattr(self.registry, "adaptive", None)
                    phase = adaptive.phase_for_model() if adaptive is not None else "main"
                    phase_limit = 6 if phase == "refinement" else 2 if phase == "reflection" else self.config.budgets["max_steps"]
                    phase_steps = adaptive.steps_by_phase[phase] if adaptive is not None else self.steps
                    if phase_steps >= phase_limit:
                        if adaptive is not None and phase in {"refinement", "reflection"}:
                            if phase == "reflection":
                                adaptive.fail_open_reflection("reflection step budget exhausted")
                            else:
                                message = adaptive.fail_open_refinement("step budget exhausted")
                                self.context.add(ModelMessage("system", message))
                            continue
                        stop_reason = "step_budget"
                        break
                    if phase == "main":
                        if self.steps >= self.config.budgets["max_steps"]:
                            stop_reason = "step_budget"
                            break
                        self.steps += 1
                    if adaptive is not None:
                        adaptive.steps_by_phase[phase] += 1
                    try:
                        response = self._call_model()
                    except Exception as exc:
                        if adaptive is not None and phase in {"refinement", "reflection"}:
                            if phase == "reflection":
                                adaptive.fail_open_reflection(f"model failure: {type(exc).__name__}")
                            else:
                                message = adaptive.fail_open_refinement(f"model failure: {type(exc).__name__}")
                                self.context.add(ModelMessage("system", message))
                            self.warnings.append(f"{phase} failed open: {type(exc).__name__}")
                            continue
                        raise
                    self.run_store.event("model_response", sequence=self.steps, phase=phase, response=response.to_dict())
                    for key, value in response.usage.items():
                        self.usage[key] = self.usage.get(key, 0) + value
                    if adaptive is not None:
                        adaptive.add_usage(phase, response.usage)
                    self.context.add(ModelMessage("assistant", response.text, response.tool_calls))
                    if response.finish_reason == "refusal":
                        stop_reason = "model_refusal"
                        break
                    if not response.tool_calls:
                        self.invalid_outputs += 1
                        self.consecutive_invalid_outputs += 1
                        self.pending_tool_calls = ()
                        self.next_tool_index = 0
                        self.next_state = "call_model"
                        self._checkpoint(self.next_state)
                        if (
                            self.consecutive_invalid_outputs >= self.config.budgets["max_consecutive_invalid_outputs"]
                            or self.invalid_outputs >= self.config.budgets["max_total_invalid_outputs"]
                        ):
                            stop_reason = "invalid_output_limit"
                            break
                        continue
                    self.consecutive_invalid_outputs = 0
                    self.pending_tool_calls = response.tool_calls
                    self.next_tool_index = 0
                    self.next_state = "execute"
                    self._checkpoint(self.next_state)
                    self._interrupt("after_model_checkpoint")

                while self.next_state == "execute":
                    if self.next_tool_index >= len(self.pending_tool_calls):
                        raise ValueError("execute state has no pending tool call")
                    adaptive = getattr(self.registry, "adaptive", None)
                    pending_phase = adaptive.phase_for_model() if adaptive is not None else "main"
                    phase_tool_limit = 12 if pending_phase == "refinement" else 2 if pending_phase == "reflection" else self.config.budgets["max_tool_calls"]
                    phase_tool_calls = adaptive.tools_by_phase[pending_phase] if adaptive is not None else self.tool_calls
                    if phase_tool_calls >= phase_tool_limit:
                        if adaptive is not None and pending_phase in {"refinement", "reflection"}:
                            if pending_phase == "reflection":
                                adaptive.fail_open_reflection("reflection tool budget exhausted")
                            else:
                                message = adaptive.fail_open_refinement("tool budget exhausted")
                                self.context.add(ModelMessage("system", message))
                            self.pending_tool_calls = ()
                            self.next_tool_index = 0
                            self.next_state = "call_model"
                            break
                        stop_reason = "tool_budget"
                        break
                    call = self.pending_tool_calls[self.next_tool_index]
                    before = self.workspace.diff_hash()
                    fingerprint = hashlib.sha256(json.dumps({"name": call.name, "arguments": call.arguments, "diff": before}, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
                    result = self.registry.execute(call.name, call.arguments)
                    if adaptive is None or pending_phase == "main":
                        self.tool_calls += 1
                    if not result.ok and result.error and result.error["kind"] in {"invalid_arguments", "unknown_tool"}:
                        self.invalid_outputs += 1
                        self.consecutive_invalid_outputs += 1
                    else:
                        self.consecutive_invalid_outputs = 0
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
                    refinement = self.registry.refinement
                    if (
                        call.name == "record_requirement_baseline"
                        and result.ok
                        and refinement is not None
                        and not refinement.context_injected
                    ):
                        self.context.add(ModelMessage("system", refinement.context_message()))
                        refinement.context_injected = True
                    adaptive = getattr(self.registry, "adaptive", None)
                    if adaptive is not None:
                        tool_phase = "refinement" if call.name == "record_requirement_brief" or adaptive.phase == "refining" else "reflection" if call.name == "reflect_on_patch" else "main"
                        adaptive.tools_by_phase[tool_phase] += 1
                        adaptive.observe_tool(call.name, result.ok, result.error, after != before)
                    if call.name == "record_requirement_brief" and result.ok and adaptive is not None:
                        self.context.add(ModelMessage("system", adaptive.brief_message()))
                    self.run_store.event("tool_result", sequence=self.tool_calls, phase=tool_phase if adaptive is not None else "main", call_id=call.call_id, result=result.to_dict())
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
                    if (
                        self.consecutive_invalid_outputs >= self.config.budgets["max_consecutive_invalid_outputs"]
                        or self.invalid_outputs >= self.config.budgets["max_total_invalid_outputs"]
                    ):
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
        atomic_json(self.run_store.path / "workspace-after.json", {"base_commit": self.workspace.base_commit, "diff_sha256": self.workspace.diff_hash()})
        names = [
            "agent.patch", "events.jsonl", "prompt.snapshot.txt", "result.json",
            "tool-schemas.json", "workspace-after.json", "workspace-before.json",
        ]
        refinement = self.registry.refinement
        if refinement is not None:
            atomic_json(self.run_store.path / "requirement-baseline.json", refinement.baseline or {})
            atomic_json(self.run_store.path / "refinement-trace.json", refinement.trace())
            names.extend(("requirement-baseline.json", "refinement-trace.json"))
        adaptive = getattr(self.registry, "adaptive", None)
        if adaptive is not None:
            atomic_json(self.run_store.path / "requirement-brief.json", adaptive.brief or {})
            atomic_json(self.run_store.path / "refinement-trace.json", adaptive.trace())
            atomic_json(self.run_store.path / "requirement-baseline.json", {"ontology_version": "coding-requirement-ontology-v1", "mode": adaptive.route.mode, "audit_expansion": adaptive.trace()})
            names.extend(("requirement-baseline.json", "requirement-brief.json", "refinement-trace.json"))
        lines = [f"{hashlib.sha256((self.run_store.path / name).read_bytes()).hexdigest()}  {name}" for name in names]
        atomic_text(self.run_store.path / "checksums.sha256", "\n".join(lines) + "\n")
        atomic_text(self.run_store.path / "COMPLETE", "complete\n")
        return result
