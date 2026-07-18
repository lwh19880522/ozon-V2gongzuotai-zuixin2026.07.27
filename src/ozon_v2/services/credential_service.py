from __future__ import annotations

from ozon_v2.adapters.fs_repo import FsRepo
from ozon_v2.adapters.credential_prompt import CredentialPromptLauncher
from ozon_v2.app.result import Result
from ozon_v2.domain.credentials import SellerCredentials, validate_credentials


class CredentialService:
    def __init__(self, repo: FsRepo | None = None) -> None:
        self.repo = repo or FsRepo()

    def create_template(self) -> Result:
        self.repo.initialize_runtime()
        template_path = self.repo.write_credentials_template()
        status = self.repo.credential_status().to_dict()
        return Result.success(
            "credentials.template_ready",
            "Credential template is ready. Fill runtime local credentials before full store tests.",
            {
                "template_path": str(template_path),
                "credentials_path": status["credentials_path"],
                "configured": status["configured"],
            },
        )

    def save_credentials(self, client_id: str, api_key: str) -> Result:
        errors = validate_credentials(client_id, api_key)
        if errors:
            return Result.failure("credentials.invalid", "Seller credentials are incomplete.", errors=errors)
        credentials = SellerCredentials(client_id=client_id.strip(), api_key=api_key.strip())
        self.repo.save_credentials(credentials)
        return Result.success(
            "credentials.saved",
            "Seller credentials saved to runtime config. API key is masked in all responses.",
            self.repo.credential_status().to_dict(),
        )

    def bind_store(self, client_id: str, api_key: str) -> Result:
        existing = self.repo.load_credentials()
        resolved_client_id = client_id.strip() or (existing.client_id if existing else "")
        resolved_api_key = api_key.strip()
        reused_history = False
        if not resolved_api_key and existing and resolved_client_id == existing.client_id:
            resolved_api_key = existing.api_key
            reused_history = True
        errors = validate_credentials(resolved_client_id, resolved_api_key)
        if errors:
            return Result.failure(
                "store_binding.invalid",
                "Store authorization binding requires store ID and API key. Leave API key empty only when reusing the same historical store.",
                errors=errors,
                data=self.repo.credential_status().to_dict(),
            )
        result = self.save_credentials(resolved_client_id, resolved_api_key)
        if not result.ok:
            return result
        status = result.data
        status["reused_history"] = reused_history
        return Result.success(
            "store_binding.bound",
            "Store authorization binding saved. Historical API key was reused." if reused_history else "Store authorization binding saved.",
            status,
        )

    def status(self) -> Result:
        status = self.repo.credential_status().to_dict()
        code = "credentials.configured" if status["configured"] else "credentials.missing"
        message = (
            "Seller credentials are configured."
            if status["configured"]
            else "Seller credentials are missing; full store dedupe cannot run yet."
        )
        return Result.success(code, message, status)

    def open_assistant(self, *, force: bool = False) -> Result:
        launch = CredentialPromptLauncher(self.repo).open_or_focus(force=force)
        ok = launch.code in {
            "credential_assistant.not_needed",
            "credential_assistant.opened",
            "credential_assistant.already_open",
            "credential_assistant.cooldown",
        }
        if ok:
            return Result.success(launch.code, launch.message, launch.to_dict())
        return Result.failure(launch.code, launch.message, data=launch.to_dict())
