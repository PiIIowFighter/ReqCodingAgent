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
        return cls(root, cache, artifacts, os.environ.get("EVALSYS_WSL_PYTHON", "python3.11"))

    @staticmethod
    def docker_prefix(platform_name: str) -> list[str]:
        return ["wsl.exe", "--", "docker"] if platform_name == "win32" else ["docker"]
