from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reqagent.config import AgentConfig

from .errors import EvalError


@dataclass(frozen=True)
class AgentRunRequest:
    task_text: str
    repository: Path
    base_commit: str

    @classmethod
    def from_public_case(cls, case: dict[str, Any], repository: Path) -> "AgentRunRequest":
        allowed = {"problem_statement", "base_commit"}
        missing = sorted(allowed - set(case))
        if missing:
            raise EvalError(f"Public case is missing Agent fields: {missing}")
        return cls(str(case["problem_statement"]), repository.resolve(), str(case["base_commit"]))

    def to_agent_input(self) -> dict[str, Any]:
        return {"task": self.task_text, "repository": str(self.repository), "base_commit": self.base_commit}


def preflight_agent_config(path: Path, *, confirmed: bool) -> AgentConfig:
    if not confirmed:
        raise EvalError("Agent execution requires explicit confirmation", category="invalid")
    config = AgentConfig.load(path)
    config.validate(live=True)
    return config
