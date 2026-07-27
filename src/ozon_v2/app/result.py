from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Result:
    ok: bool
    code: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @classmethod
    def success(cls, code: str, message: str, data: dict[str, Any] | None = None) -> "Result":
        return cls(ok=True, code=code, message=message, data=data or {})

    @classmethod
    def failure(
        cls,
        code: str,
        message: str,
        errors: list[str] | None = None,
        data: dict[str, Any] | None = None,
    ) -> "Result":
        return cls(ok=False, code=code, message=message, data=data or {}, errors=errors or [])

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code,
            "message": self.message,
            "data": self.data,
            "errors": self.errors,
        }

