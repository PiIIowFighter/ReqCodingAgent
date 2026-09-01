from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Sensitive content patterns
_SENSITIVE_PATTERN = re.compile(
    r"(?i)(?:[a-z]:[\\/]|/)(?:[^\s]+[\\/])+[^\s]+|"  # Absolute paths
    r"\b(api[_-]?key|auth(?:entication)?[_-]?token|password|secret|bearer)\b\s*[:=]\s*\S+|"  # Credentials
    r"reasoning|encrypted_content",  # Model internal content
    re.IGNORECASE
)


def _sanitize_text(text: str, max_length: int = 2000) -> str:
    """Sanitize text by removing sensitive information."""
    if not text:
        return text

    # Check for suspected credentials
    if re.search(r"\b(sk-[a-zA-Z0-9]{20,}|Bearer\s+[a-zA-Z0-9+/=]{20,})", text):
        raise ValueError("Text contains suspected API key or token. Do not submit credentials.")

    sanitized = _SENSITIVE_PATTERN.sub("[redacted]", text)
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length - 1] + "…"
    return sanitized


def _load_ontology() -> dict[str, Any]:
    """Load the frozen requirement ontology."""
    ontology_path = PROJECT_ROOT / "configs/frozen/baseline-v3/requirement-ontology.json"
    return json.loads(ontology_path.read_text(encoding="utf-8"))


def _get_all_slots(ontology: dict[str, Any]) -> set[str]:
    """Extract all valid slot IDs from ontology."""
    slots = set()
    for category_slots in ontology["ontology"].values():
        slots.update(category_slots)
    return slots


def _build_ontology_context(ontology: dict[str, Any]) -> str:
    """Build ontology context for the interview prompt."""
    categories = []
    for category_id, slots in ontology["ontology"].items():
        slot_list = ", ".join(slots)
        categories.append(f"  {category_id}: [{slot_list}]")
    return "# Frozen Coding Requirement Ontology\n\n" + "\n".join(categories)


def _system_prompt() -> str:
    """Load the interview system prompt."""
    prompt_path = PROJECT_ROOT / "prompts/demo/requirement-interview.txt"
    return prompt_path.read_text(encoding="utf-8")


class ModelAdapter(Protocol):
    """Protocol for model adapters."""
    def complete(self, request: Any) -> Any:
        """Complete a model request."""
        ...


@dataclass
class InterviewTurn:
    """One turn of the interview."""
    turn_id: str
    question: str
    selected_slot_ids: list[str]
    selection_reason: str
    answer: str | None = None
    slot_updates: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


@dataclass
class RequirementBaseline:
    """The synthesized requirement baseline."""
    original_request: str
    refined_summary: str
    requirements: list[str]
    acceptance_criteria: list[str]
    constraints: list[str]
    excluded_scope: list[str]
    assumptions: list[str]
    unresolved_items: list[str]
    slot_states: dict[str, dict[str, Any]]
    confirmed_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_request": _sanitize_text(self.original_request),
            "refined_summary": _sanitize_text(self.refined_summary),
            "requirements": [_sanitize_text(r, 500) for r in self.requirements],
            "acceptance_criteria": [_sanitize_text(c, 500) for c in self.acceptance_criteria],
            "constraints": [_sanitize_text(c, 500) for c in self.constraints],
            "excluded_scope": [_sanitize_text(e, 500) for e in self.excluded_scope],
            "assumptions": [_sanitize_text(a, 500) for a in self.assumptions],
            "unresolved_items": [_sanitize_text(u, 500) for u in self.unresolved_items],
            "slot_states": self.slot_states,
            "confirmed_at": self.confirmed_at,
        }

    def to_task_description(self) -> str:
        """Convert baseline to a task description for the coding agent."""
        parts = [
            f"# Original Request\n{self.original_request}\n",
            f"# Refined Summary\n{self.refined_summary}\n",
        ]

        if self.requirements:
            parts.append("# Requirements\n" + "\n".join(f"- {r}" for r in self.requirements) + "\n")

        if self.acceptance_criteria:
            parts.append("# Acceptance Criteria\n" + "\n".join(f"- {c}" for c in self.acceptance_criteria) + "\n")

        if self.constraints:
            parts.append("# Constraints\n" + "\n".join(f"- {c}" for c in self.constraints) + "\n")

        if self.excluded_scope:
            parts.append("# Excluded Scope\n" + "\n".join(f"- {e}" for e in self.excluded_scope) + "\n")

        if self.assumptions:
            parts.append("# Assumptions\n" + "\n".join(f"- {a}" for a in self.assumptions) + "\n")

        if self.unresolved_items:
            parts.append("# Unresolved (not blocking)\n" + "\n".join(f"- {u}" for u in self.unresolved_items) + "\n")

        return "\n".join(parts)


class InterviewSession:
    """Manages an interactive requirement interview session."""

    def __init__(self, original_request: str, adapter: ModelAdapter, ontology_version: str):
        """
        Initialize an interview session.

        Args:
            original_request: The original vague user request
            adapter: A ModelAdapter instance (e.g., OpenAIResponsesAdapter)
            ontology_version: Version hash of the frozen ontology being used
        """
        self.original_request = _sanitize_text(original_request)
        self.adapter = adapter
        self.ontology_version = ontology_version
        self.ontology = _load_ontology()
        self.valid_slots = _get_all_slots(self.ontology)
        self.turns: list[InterviewTurn] = []
        self.baseline: RequirementBaseline | None = None
        self.max_turns = 3
        self.min_turns = 2
        self.actual_models: list[str] = []
        self.used_call_ids: set[str] = set()

        # Build messages for ModelRequest
        system_content = _system_prompt() + "\n\n" + _build_ontology_context(self.ontology)

        from reqagent.model import ModelMessage
        self.messages: list[ModelMessage] = [
            ModelMessage(role="system", text=system_content),
            ModelMessage(role="user", text=f"Original user request: {self.original_request}\n\nBegin the requirement interview. Ask your first clarifying question.")
        ]

    def _tool_schemas(self) -> tuple:
        """Define the tools available to the interview agent based on current turn count."""
        from reqagent.model import ToolDefinition

        tools = []

        # Only allow ask_clarification if we haven't completed 3 turns
        if len(self.turns) < self.max_turns:
            tools.append(ToolDefinition(
                name="ask_clarification",
                description="Ask one clarifying question targeting specific ontology slots",
                input_schema={
                    "type": "object",
                    "required": ["question", "slot_ids", "selection_reason"],
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "Natural language question for the user"
                        },
                        "slot_ids": {
                            "type": "array",
                            "description": "Ontology slot IDs this question explores",
                            "items": {"type": "string"},
                            "minItems": 1
                        },
                        "selection_reason": {
                            "type": "string",
                            "description": "Brief explanation of why these slots are valuable now"
                        }
                    },
                    "additionalProperties": False
                }
            ))

        # Allow finish_interview if we've completed at least 2 turns
        if len(self.turns) >= self.min_turns:
            tools.append(ToolDefinition(
                name="finish_interview",
                description="Synthesize the confirmed requirement baseline (call after 2-3 turns)",
                input_schema={
                    "type": "object",
                    "required": ["original_request", "refined_summary", "requirements", "acceptance_criteria",
                               "constraints", "excluded_scope", "assumptions", "unresolved_items", "slot_states"],
                    "properties": {
                        "original_request": {"type": "string"},
                        "refined_summary": {"type": "string"},
                        "requirements": {"type": "array", "items": {"type": "string"}},
                        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                        "constraints": {"type": "array", "items": {"type": "string"}},
                        "excluded_scope": {"type": "array", "items": {"type": "string"}},
                        "assumptions": {"type": "array", "items": {"type": "string"}},
                        "unresolved_items": {"type": "array", "items": {"type": "string"}},
                        "slot_states": {
                            "type": "object",
                            "description": "Map of slot_id to {state, value, evidence}",
                            "additionalProperties": {
                                "type": "object",
                                "required": ["state"],
                                "properties": {
                                    "state": {"type": "string", "enum": ["confirmed", "rejected", "unresolved", "unexplored"]},
                                    "value": {"type": "string"},
                                    "evidence": {"type": "string"}
                                },
                                "additionalProperties": False
                            }
                        }
                    },
                    "additionalProperties": False
                }
            ))

        return tuple(tools)

    def generate_next_question(self) -> InterviewTurn | None:
        """Generate the next clarifying question. Returns None if interview is complete."""
        if len(self.turns) > self.max_turns:
            raise ValueError("Maximum interview turns exceeded")

        # Build ModelRequest
        from reqagent.model import ModelRequest

        request = ModelRequest(
            messages=tuple(self.messages),
            tools=self._tool_schemas(),
            max_output_tokens=4000,
            timeout_seconds=300
        )

        # Call the adapter
        try:
            response = self.adapter.complete(request)
        except Exception as exc:
            raise ValueError(f"Model request failed: {exc}") from exc

        # Record actual model
        if response.actual_model:
            self.actual_models.append(response.actual_model)

        # Verify exactly one tool call
        if len(response.tool_calls) != 1:
            raise ValueError(f"Expected exactly 1 tool call, got {len(response.tool_calls)}")

        tool_call = response.tool_calls[0]

        # Check for duplicate call_id
        if tool_call.call_id in self.used_call_ids:
            raise ValueError(f"Duplicate call_id: {tool_call.call_id}")
        self.used_call_ids.add(tool_call.call_id)

        # Add assistant message to history
        from reqagent.model import ModelMessage
        self.messages.append(ModelMessage(
            role="assistant",
            text=response.text,
            tool_calls=(tool_call,)
        ))

        if tool_call.name == "ask_clarification":
            # Validate slot_ids
            slot_ids = tool_call.arguments.get("slot_ids", [])
            if not slot_ids:
                raise ValueError("ask_clarification requires at least one slot_id")

            invalid_slots = [sid for sid in slot_ids if sid not in self.valid_slots]
            if invalid_slots:
                raise ValueError(f"Invalid slot IDs: {invalid_slots}. Must be from frozen ontology.")

            # Sanitize question and reason
            question = _sanitize_text(tool_call.arguments["question"], 500)
            selection_reason = _sanitize_text(tool_call.arguments["selection_reason"], 300)

            turn = InterviewTurn(
                turn_id=tool_call.call_id,
                question=question,
                selected_slot_ids=slot_ids,
                selection_reason=selection_reason
            )
            self.turns.append(turn)
            return turn

        elif tool_call.name == "finish_interview":
            if len(self.turns) < self.min_turns:
                raise ValueError(f"Cannot finish interview before {self.min_turns} turns")

            # Validate original_request matches
            if tool_call.arguments["original_request"] != self.original_request:
                raise ValueError("original_request in baseline does not match session original_request")

            # Validate slot_states
            slot_states = tool_call.arguments.get("slot_states", {})
            invalid_slots = [sid for sid in slot_states.keys() if sid not in self.valid_slots]
            if invalid_slots:
                raise ValueError(f"Invalid slot IDs in slot_states: {invalid_slots}")

            # Validate list lengths and total size
            args = tool_call.arguments
            for key in ["requirements", "acceptance_criteria", "constraints", "excluded_scope", "assumptions", "unresolved_items"]:
                items = args.get(key, [])
                if not isinstance(items, list):
                    raise ValueError(f"{key} must be a list")
                if len(items) > 50:
                    raise ValueError(f"{key} too long: {len(items)} items")

            total_chars = sum(len(str(v)) for v in args.values())
            if total_chars > 50000:
                raise ValueError(f"Baseline too large: {total_chars} characters")

            # Create baseline (not confirmed yet - user must explicitly confirm)
            self.baseline = RequirementBaseline(
                original_request=self.original_request,
                refined_summary=_sanitize_text(args["refined_summary"], 500),
                requirements=[_sanitize_text(r, 500) for r in args["requirements"]],
                acceptance_criteria=[_sanitize_text(c, 500) for c in args["acceptance_criteria"]],
                constraints=[_sanitize_text(c, 500) for c in args["constraints"]],
                excluded_scope=[_sanitize_text(e, 500) for e in args["excluded_scope"]],
                assumptions=[_sanitize_text(a, 500) for a in args["assumptions"]],
                unresolved_items=[_sanitize_text(u, 500) for u in args["unresolved_items"]],
                slot_states=slot_states,
                confirmed_at=None  # Not confirmed yet
            )

            return None  # Signals completion

        else:
            raise ValueError(f"Unknown tool: {tool_call.name}")

    def submit_answer(self, turn_id: str, answer: str) -> InterviewTurn | None:
        """
        Submit user's answer to the current question.

        Returns the next turn, or None if interview is complete.
        """
        # Find the turn
        turn = None
        for t in self.turns:
            if t.turn_id == turn_id:
                turn = t
                break

        if not turn:
            raise ValueError(f"Unknown turn_id: {turn_id}")

        if turn.answer is not None:
            raise ValueError(f"Turn {turn_id} already answered")

        # Sanitize and validate answer
        answer = answer.strip()
        if not answer:
            raise ValueError("Answer cannot be empty")

        answer = _sanitize_text(answer, 2000)

        # Record answer
        turn.answer = answer

        # Add tool result to messages
        from reqagent.model import ModelMessage
        self.messages.append(ModelMessage(
            role="tool",
            tool_results=({"call_id": turn_id, "output": answer},)
        ))

        # Force finish after max turns
        if len(self.turns) >= self.max_turns:
            # Add instruction to finish
            self.messages.append(ModelMessage(
                role="user",
                text=f"You have completed {self.max_turns} clarification rounds. Now call finish_interview to synthesize the requirement baseline."
            ))

        # Generate next question or finish
        return self.generate_next_question()

    def to_transcript(self) -> dict[str, Any]:
        """Export interview transcript for artifact storage."""
        return {
            "original_request": _sanitize_text(self.original_request),
            "ontology_version": self.ontology_version,
            "turns": [
                {
                    "turn_id": t.turn_id,
                    "question": _sanitize_text(t.question, 500),
                    "selected_slot_ids": t.selected_slot_ids,
                    "selection_reason": _sanitize_text(t.selection_reason, 300),
                    "answer": _sanitize_text(t.answer, 2000) if t.answer else None,
                    "slot_updates": t.slot_updates,
                    "timestamp": t.timestamp
                }
                for t in self.turns
            ],
            "baseline": self.baseline.to_dict() if self.baseline else None,
            "completed": self.baseline is not None
        }
