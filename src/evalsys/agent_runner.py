from __future__ import annotations

import subprocess
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
        prompt_key = "prompt" if "prompt" in case else "problem_statement"
        missing = [key for key in (prompt_key, "base_commit") if key not in case]
        if missing:
            raise EvalError(f"Public case is missing Agent fields: {missing}")
        return cls(str(case[prompt_key]), repository.resolve(), str(case["base_commit"]))

    def to_agent_input(self) -> dict[str, Any]:
        return {"task": self.task_text, "repository": str(self.repository), "base_commit": self.base_commit}

    def verify_repository(self) -> None:
        completed = subprocess.run(
            ["git", "-C", str(self.repository), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode or completed.stdout.strip() != self.base_commit:
            raise EvalError("task repository base commit does not match the frozen case", category="invalid")
        status = subprocess.run(
            ["git", "-C", str(self.repository), "status", "--porcelain"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if status.returncode or status.stdout.strip():
            raise EvalError("task repository must be a clean Git workspace", category="invalid")


def preflight_agent_config(path: Path, *, confirmed: bool) -> AgentConfig:
    if not confirmed:
        raise EvalError("Agent execution requires explicit confirmation", category="invalid")
    config = AgentConfig.load(path)
    config.validate(live=True)
    return config
