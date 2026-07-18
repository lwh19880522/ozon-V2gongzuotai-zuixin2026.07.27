from __future__ import annotations

from ozon_v2.adapters.fs_repo import FsRepo
from ozon_v2.domain.models import ExistingStoreProduct
from ozon_v2.services.credential_service import CredentialService
from ozon_v2.services.run_service import RunService

from tests.helpers import RuntimeTestCase


class CredentialAssistantTests(RuntimeTestCase):
    def test_template_is_created_in_runtime_config(self) -> None:
        repo = FsRepo(self.context)

        result = CredentialService(repo).create_template()

        self.assertTrue(result.ok)
        self.assertTrue((self.context.runtime_root / "config" / "seller_credentials.template.json").exists())
        self.assertEqual(str(self.context.runtime_root / "config" / "seller_credentials.local.json"), result.data["credentials_path"])

    def test_status_missing_credentials_is_safe(self) -> None:
        repo = FsRepo(self.context)

        result = CredentialService(repo).status()

        self.assertTrue(result.ok)
        self.assertEqual("credentials.missing", result.code)
        self.assertFalse(result.data["configured"])
        self.assertIsNone(result.data["api_key_masked"])

    def test_save_credentials_masks_api_key_in_response(self) -> None:
        repo = FsRepo(self.context)
        secret = "seller-api-key-super-secret"

        result = CredentialService(repo).save_credentials(client_id="client-123", api_key=secret)

        self.assertTrue(result.ok)
        self.assertEqual("credentials.saved", result.code)
        self.assertEqual("client-123", result.data["client_id"])
        self.assertNotIn(secret, str(result.to_dict()))
        self.assertTrue(result.data["api_key_masked"].endswith("cret"))

    def test_bind_store_saves_new_store_authorization_without_echoing_secret(self) -> None:
        repo = FsRepo(self.context)
        secret = "seller-api-key-super-secret"

        result = CredentialService(repo).bind_store(client_id="client-123", api_key=secret)

        self.assertTrue(result.ok)
        self.assertEqual("store_binding.bound", result.code)
        self.assertEqual("client-123", result.data["client_id"])
        self.assertFalse(result.data["reused_history"])
        self.assertNotIn(secret, str(result.to_dict()))

    def test_bind_store_reuses_history_when_store_is_unchanged_and_key_is_empty(self) -> None:
        repo = FsRepo(self.context)
        service = CredentialService(repo)
        service.bind_store(client_id="client-123", api_key="seller-api-key-super-secret")

        result = service.bind_store(client_id="client-123", api_key="")

        self.assertTrue(result.ok)
        self.assertEqual("store_binding.bound", result.code)
        self.assertTrue(result.data["reused_history"])
        self.assertEqual("client-123", result.data["client_id"])

    def test_bind_store_requires_key_when_switching_store(self) -> None:
        repo = FsRepo(self.context)
        service = CredentialService(repo)
        service.bind_store(client_id="client-123", api_key="seller-api-key-super-secret")

        result = service.bind_store(client_id="client-456", api_key="")

        self.assertFalse(result.ok)
        self.assertEqual("store_binding.invalid", result.code)

    def test_doctor_reports_credentials_status_without_secret(self) -> None:
        repo = FsRepo(self.context)
        secret = "seller-api-key-super-secret"
        CredentialService(repo).save_credentials(client_id="client-123", api_key=secret)

        result = RunService(repo).doctor()

        self.assertTrue(result.ok)
        self.assertTrue(result.data["seller_credentials_configured"])
        self.assertEqual("client-123", result.data["seller_client_id"])
        self.assertNotIn(secret, str(result.to_dict()))
        self.assertFalse(result.data["full_store_dedupe_ready"])

        repo.replace_existing_products(
            [
                ExistingStoreProduct(
                    store_product_id="existing-1",
                    title="Existing product",
                    normalized_identity_key="existingproduct",
                )
            ]
        )
        ready_result = RunService(repo).doctor()

        self.assertTrue(ready_result.data["full_store_dedupe_ready"])
