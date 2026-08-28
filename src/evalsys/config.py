from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import EvalError


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class Settings:
    project_root: Path
    cache_root: Path
    artifact_root: Path
    wsl_python: str = "python3.11"

    def __post_init__(self) -> None:
        if _inside(self.cache_root, self.project_root):
            raise EvalError("External cache must be outside the project root", hint="Set EVALSYS_CACHE_ROOT to ~/.cache/reqcodingagent")

    @classmethod
    def from_env(cls, project_root: Path | None = None) -> "Settings":
        root = (project_root or Path.cwd()).resolve()
        cache = Path(os.environ.get("EVALSYS_CACHE_ROOT", "~/.cache/reqcodingagent")).expanduser().resolve()
        artifacts = Path(os.environ.get("EVALSYS_ARTIFACT_ROOT", root / "artifacts")).expanduser().resolve()
        wsl_python = os.environ.get("EVALSYS_WSL_PYTHON", "python3.11")
        if len(wsl_python) >= 3 and wsl_python[1:3] in {":/", ":\\"}:
            raise EvalError("EVALSYS_WSL_PYTHON contains a Windows drive path, likely rewritten by MSYS", hint="Set it to a command name such as python3.11")
        return cls(root, cache, artifacts, wsl_python)

    @staticmethod
    def docker_prefix(platform_name: str) -> list[str]:
        return ["wsl.exe", "--", "docker"] if platform_name == "win32" else ["docker"]
