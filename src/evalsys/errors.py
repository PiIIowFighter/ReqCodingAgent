from __future__ import annotations


class EvalError(RuntimeError):
    def __init__(self, message: str, *, hint: str = "", category: str = "invalid") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.category = category

    def __str__(self) -> str:
        return f"{self.message}{f' Hint: {self.hint}' if self.hint else ''}"
