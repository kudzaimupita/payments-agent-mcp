"""Domain errors carry a stable machine-readable code for tools and tests."""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            out["details"] = self.details
        return out


class NotFoundError(DomainError):
    def __init__(self, kind: str, ident: str) -> None:
        super().__init__("NOT_FOUND", f"{kind} {ident} does not exist", kind=kind, id=ident)
