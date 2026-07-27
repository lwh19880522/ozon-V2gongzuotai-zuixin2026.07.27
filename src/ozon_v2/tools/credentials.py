from __future__ import annotations

from ozon_v2.services.credential_service import CredentialService


def ozon_v2_credentials_template() -> dict:
    return CredentialService().create_template().to_dict()


def ozon_v2_credentials_status() -> dict:
    return CredentialService().status().to_dict()


def ozon_v2_open_credential_assistant(force: bool = False) -> dict:
    return CredentialService().open_assistant(force=force).to_dict()


def ozon_v2_save_credentials(store_id: str, api_key: str) -> dict:
    return CredentialService().save_credentials(client_id=store_id, api_key=api_key).to_dict()
