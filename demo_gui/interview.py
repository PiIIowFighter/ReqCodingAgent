from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_ontology() -> dict[str, Any]:
    """Load the frozen requirement ontology."""
    ontology_path = PROJECT_ROOT / "configs/frozen/baseline-v3/requirement-ontology.json"
    return json.loads(ontology_path.read_text(encoding="utf-8"))


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
    confirmed_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_request": self.original_request,
            "refined_summary": self.refined_summary,
            "requirements": self.requirements,
            "acceptance_criteria": self.acceptance_criteria,
            "constraints": self.constraints,
            "excluded_scope": self.excluded_scope,
            "assumptions": self.assumptions,
            "unresolved_items": self.unresolved_items,
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

    def __init__(self, original_request: str, client, model: str, ontology_version: str):
        """
        Initialize an interview session.

        Args:
            original_request: The original vague user request
            client: OpenAI client instance
            model: Model name to use (e.g., gpt-4o-mini)
            ontology_version: Version hash of the frozen ontology being used
        """
        self.original_request = original_request
        self.client = client
        self.model = model
        self.ontology_version = ontology_version
        self.ontology = _load_ontology()
        self.turns: list[InterviewTurn] = []
        self.baseline: RequirementBaseline | None = None
        self.max_turns = 3
        self.min_turns = 2
        self.actual_model: str | None = None

        # Build system message with ontology context
        system_content = _system_prompt() + "\n\n" + _build_ontology_context(self.ontology)

        # Wire history for Responses protocol
        self.instructions = system_content
        self.wire_input: list[dict[str, Any]] = []

        # Add initial user message
        initial_message = f"Original user request: {original_request}\n\nBegin the requirement interview. Ask your first clarifying question."
        self.wire_input.append({"role": "user", "content": initial_message})

    def _tool_schemas(self) -> list[dict[str, Any]]:
        """Define the tools available to the interview agent."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "ask_clarification",
                    "description": "Ask one clarifying question targeting specific ontology slots",
                    "strict": True,
                    "parameters": {
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
                                "items": {"type": "string"}
                            },
                            "selection_reason": {
                                "type": "string",
                                "description": "Brief explanation of why these slots are valuable now"
                            }
                        },
                        "additionalProperties": False
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "finish_interview",
                    "description": "Synthesize the confirmed requirement baseline (call after 2-3 turns)",
                    "strict": True,
                    "parameters": {
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
                }
            }
        ]

    def generate_next_question(self) -> InterviewTurn:
        """Generate the next clarifying question."""
        if len(self.turns) > self.max_turns:
            raise ValueError("Maximum interview turns exceeded")

        # Call the model using Responses protocol
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                input=list(self.wire_input),
                instructions=self.instructions,
                tools=self._tool_schemas(),
                tool_choice="required",
                store=False
            )
        except Exception as exc:
            raise ValueError(f"Model request failed: {exc}") from exc

        # Record actual model
        self.actual_model = getattr(response, "model", None)

        # Extract output
        output = getattr(response, "output", [])
        if not output:
            raise ValueError("Model did not return any output")

        # Preserve full wire output for continuity
        self.wire_input.extend([self._item_to_wire(item) for item in output])

        # Find tool call
        tool_call = None
        for item in output:
            item_type = getattr(item, "type", None)
            if item_type == "function_call":
                tool_call = item
                break

        if not tool_call:
            raise ValueError("Model did not call a tool")

        tool_name = getattr(tool_call, "name", None)
        arguments_json = getattr(tool_call, "arguments", "{}")
        call_id = getattr(tool_call, "call_id", "")

        try:
            tool_input = json.loads(arguments_json)
        except json.JSONDecodeError as exc:
            raise ValueError("Tool arguments are not valid JSON") from exc

        if tool_name == "ask_clarification":
            turn = InterviewTurn(
                turn_id=call_id,
                question=tool_input["question"],
                selected_slot_ids=tool_input["slot_ids"],
                selection_reason=tool_input["selection_reason"]
            )
            self.turns.append(turn)
            return turn

        elif tool_name == "finish_interview":
            if len(self.turns) < self.min_turns:
                raise ValueError(f"Cannot finish interview before {self.min_turns} turns")

            # Create baseline
            self.baseline = RequirementBaseline(
                original_request=tool_input["original_request"],
                refined_summary=tool_input["refined_summary"],
                requirements=tool_input["requirements"],
                acceptance_criteria=tool_input["acceptance_criteria"],
                constraints=tool_input["constraints"],
                excluded_scope=tool_input["excluded_scope"],
                assumptions=tool_input["assumptions"],
                unresolved_items=tool_input["unresolved_items"],
                slot_states=tool_input["slot_states"]
            )

            return None  # Signals completion

        else:
            raise ValueError(f"Unknown tool: {tool_name}")

    @staticmethod
    def _item_to_wire(item: Any) -> dict[str, Any]:
        """Convert response output item to wire format."""
        if isinstance(item, dict):
            return dict(item)
        if hasattr(item, "model_dump"):
            return item.model_dump(exclude_none=True)
        return {key: getattr(item, key) for key in dir(item) if not key.startswith("_") and not callable(getattr(item, key))}

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

        # Record answer
        turn.answer = answer

        # Add tool result to wire input
        self.wire_input.append({
            "type": "function_call_output",
            "call_id": turn_id,
            "output": answer
        })

        # Force finish after max turns
        if len(self.turns) >= self.max_turns:
            # Add instruction to finish
            self.wire_input.append({
                "role": "user",
                "content": f"You have completed {self.max_turns} clarification rounds. Now call finish_interview to synthesize the requirement baseline."
            })

        # Generate next question or finish
        return self.generate_next_question()

    def to_transcript(self) -> dict[str, Any]:
        """Export interview transcript for artifact storage."""
        return {
            "original_request": self.original_request,
            "ontology_version": self.ontology_version,
            "turns": [
                {
                    "turn_id": t.turn_id,
                    "question": t.question,
                    "selected_slot_ids": t.selected_slot_ids,
                    "selection_reason": t.selection_reason,
                    "answer": t.answer,
                    "slot_updates": t.slot_updates,
                    "timestamp": t.timestamp
                }
                for t in self.turns
            ],
            "baseline": self.baseline.to_dict() if self.baseline else None,
            "completed": self.baseline is not None
        }
