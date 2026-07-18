from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ozon_v2.domain.models import utc_now_iso


def mask_secret(value: str, visible: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= visible:
        return "*" * len(value)
    return f"{'*' * (len(value) - visible)}{value[-visible:]}"


@dataclass
class SellerCredentials:
    client_id: str
    api_key: str
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SellerCredentials":
        return cls(
            client_id=str(payload.get("client_id", "")).strip(),
            api_key=str(payload.get("api_key", "")).strip(),
            created_at=payload.get("created_at") or utc_now_iso(),
            updated_at=payload.get("updated_at") or utc_now_iso(),
        )

    def to_private_dict(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "api_key": self.api_key,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "api_key_masked": mask_secret(self.api_key),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class CredentialStatus:
    configured: bool
    credentials_path: str
    template_path: str
    client_id: str | None = None
    api_key_masked: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "credentials_path": self.credentials_path,
            "template_path": self.template_path,
            "client_id": self.client_id,
            "api_key_masked": self.api_key_masked,
        }


def validate_credentials(client_id: str, api_key: str) -> list[str]:
    errors: list[str] = []
    if not client_id.strip():
        errors.append("client_id/store_id is required")
    if not api_key.strip():
        errors.append("api_key is required")
    if any(token in api_key.lower() for token in ["todo", "replace", "填入", "your_api_key"]):
        errors.append("api_key still looks like a placeholder")
    return errors

