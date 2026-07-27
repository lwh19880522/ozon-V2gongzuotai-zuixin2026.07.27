from __future__ import annotations

from ozon_v2.adapters.fs_repo import FsRepo
from ozon_v2.domain.credentials import SellerCredentials
from ozon_v2.domain.models import ExistingStoreProduct
from ozon_v2.services.run_service import RunService

from tests.helpers import RuntimeTestCase


class FakeCredentialPromptLauncher:
    def __init__(self) -> None:
        self.open_count = 0

    def open_or_focus(self):
        self.open_count += 1
        return FakeCredentialAssistantLaunch()


class FakeCredentialAssistantLaunch:
    def to_dict(self) -> dict:
        return {
            "code": "credential_assistant.opened",
            "state": "opened",
            "pid": 123,
        }


class SeedSamplingTests(RuntimeTestCase):
    def test_start_run_samples_without_removing_active_seeds(self) -> None:
        repo = FsRepo(self.context)
        self.save_test_credentials(repo)
        service = RunService(repo)

        result = service.start_run(target_count=2, random_seed=7)

        self.assertTrue(result.ok)
        self.assertEqual(2, len(result.data["sampled_seeds"]))
        self.assertEqual("create_ozon_collection_contract", result.data["next_action"])
        self.assertEqual(5000, len(repo.load_active_seeds()))

    def test_existing_store_dedupe_blocks_seed_before_sampling(self) -> None:
        repo = FsRepo(self.context)
        self.save_test_credentials(repo)
        repo.initialize_runtime()
        first_seed = repo.load_active_seeds()[0]
        repo.replace_existing_products(
            [
                ExistingStoreProduct(
                    store_product_id="existing-1",
                    title=first_seed.title_or_keyword,
                    normalized_identity_key=first_seed.title_or_keyword,
                )
            ]
        )

        result = RunService(repo).start_run(target_count=5000, random_seed=1)

        self.assertFalse(result.ok)
        self.assertEqual("run.insufficient_seeds", result.code)
        self.assertEqual(4999, result.data["eligible_seed_count"])

    def test_next_action_uses_bundled_queries_for_ozon_contract(self) -> None:
        repo = FsRepo(self.context)
        self.save_test_credentials(repo)
        service = RunService(repo)
        run_id = service.start_run(target_count=1, random_seed=3).data["run"]["run_id"]

        result = service.next_action(run_id)

        self.assertTrue(result.ok)
        self.assertEqual("next.ozon_collection_contract", result.code)

    def test_start_run_blocks_without_credentials(self) -> None:
        repo = FsRepo(self.context)
        prompt = FakeCredentialPromptLauncher()
        service = RunService(repo, credential_prompt_launcher=prompt)

        result = service.start_run(target_count=1, random_seed=3)

        self.assertFalse(result.ok)
        self.assertEqual("run.credentials_missing", result.code)
        self.assertFalse(result.data["configured"])
        self.assertEqual(1, prompt.open_count)
        self.assertEqual("credential_assistant.opened", result.data["credential_assistant"]["code"])

    def test_start_run_blocks_without_existing_store_dedupe_refresh(self) -> None:
        repo = FsRepo(self.context)
        repo.save_credentials(SellerCredentials(client_id="test-store-id", api_key="test-secret-api-key-1234"))

        result = RunService(repo).start_run(target_count=1, random_seed=3)

        self.assertFalse(result.ok)
        self.assertEqual("run.existing_store_dedupe_missing", result.code)
        self.assertFalse(result.data["ready"])
