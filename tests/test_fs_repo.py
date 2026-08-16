from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json

from ozon_v2.adapters.fs_repo import FsRepo
from ozon_v2.domain.models import ExistingStoreProduct, SeedProduct

from tests.helpers import RuntimeTestCase


class FsRepoTests(RuntimeTestCase):
    def test_store_refresh_preserves_uploaded_product_lineage(self) -> None:
        repo = FsRepo(self.context)
        repo.merge_existing_products(
            [
                ExistingStoreProduct(
                    store_product_id="900001",
                    title="Uploaded title",
                    normalized_identity_key="uploadedtitle",
                    offer_id_when_available="OZV2-898078910803-ABCDEF12",
                    source_seed_identity_key="source:seed-1440",
                    seed_subject_identity_key="电子桌面时钟",
                    source_ozon_product_id="1189584062",
                    source_ozon_title="Электронные настольные часы",
                    supplier_offer_id="898078910803",
                )
            ]
        )

        repo.merge_existing_products(
            [
                ExistingStoreProduct(
                    store_product_id="900001",
                    title="Seller API rewritten title",
                    normalized_identity_key="sellerapirewrittentitle",
                    offer_id_when_available="OZV2-898078910803-ABCDEF12",
                    notes="refreshed from Ozon Seller API",
                )
            ]
        )

        [stored] = repo.load_existing_products()
        self.assertEqual("Seller API rewritten title", stored.title)
        self.assertEqual("source:seed-1440", stored.source_seed_identity_key)
        self.assertEqual("电子桌面时钟", stored.seed_subject_identity_key)
        self.assertEqual("1189584062", stored.source_ozon_product_id)
        self.assertEqual("Электронные настольные часы", stored.source_ozon_title)
        self.assertEqual("898078910803", stored.supplier_offer_id)

    def test_legacy_ozv2_offer_recovers_supplier_identity(self) -> None:
        product = ExistingStoreProduct.from_dict(
            {
                "store_product_id": "900002",
                "title": "Legacy upload",
                "normalized_identity_key": "legacyupload",
                "offer_id_when_available": "OZV2-856597416777-2CD945D5",
            }
        )

        self.assertEqual("856597416777", product.supplier_offer_id)

    def test_clear_workbench_batches_removes_all_recognized_batch_directories(self) -> None:
        repo = FsRepo(self.context)
        repo.initialize_runtime()
        first = repo.create_workbench_batch_record(target_count=1)
        second = repo.create_workbench_batch_record(target_count=2)
        legacy_dir = repo.run_dir("run-legacy-clear")
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "run.json").write_text(
            json.dumps({"run_id": "run-legacy-clear", "status": "finalized"}),
            encoding="utf-8",
        )
        malformed_workbench_dir = repo.run_dir("wb-malformed-clear")
        malformed_workbench_dir.mkdir(parents=True)
        malformed_legacy_dir = repo.run_dir("run-malformed-clear")
        malformed_legacy_dir.mkdir(parents=True)
        unrelated_dir = repo.runs_dir / "manual-notes"
        unrelated_dir.mkdir()

        credentials_before = repo.credentials_template_path.read_bytes()
        seeds_before = repo.active_seed_path.read_bytes()
        dedupe_before = repo.existing_store_dedupe_path.read_bytes()

        result = repo.clear_workbench_batches()

        self.assertEqual(
            {
                first["run_id"],
                second["run_id"],
                "run-legacy-clear",
                "wb-malformed-clear",
                "run-malformed-clear",
            },
            set(result["deleted_run_ids"]),
        )
        self.assertFalse(repo.run_dir(first["run_id"]).exists())
        self.assertFalse(repo.run_dir(second["run_id"]).exists())
        self.assertFalse(legacy_dir.exists())
        self.assertFalse(malformed_workbench_dir.exists())
        self.assertFalse(malformed_legacy_dir.exists())
        self.assertTrue(unrelated_dir.exists())
        self.assertEqual(credentials_before, repo.credentials_template_path.read_bytes())
        self.assertEqual(seeds_before, repo.active_seed_path.read_bytes())
        self.assertEqual(dedupe_before, repo.existing_store_dedupe_path.read_bytes())

    def test_clear_workbench_batches_archives_product_identities_before_deletion(self) -> None:
        repo = FsRepo(self.context)
        run = repo.create_workbench_batch_record(target_count=1)
        run_id = run["run_id"]
        repo.save_ozon_collection_result(
            run_id,
            {
                "run_id": run_id,
                "ozon_candidates": [
                    {"seed_id": "seed-history", "ozon_product_id": "1189584062"}
                ],
            },
        )
        repo.save_supplier_collection_result(
            run_id,
            {
                "run_id": run_id,
                "supplier_products": [
                    {
                        "seed_id": "seed-history",
                        "offer_id": "898078910803",
                        "supplier_url": "https://detail.1688.com/offer/898078910803.html",
                    }
                ],
            },
        )

        repo.clear_workbench_batches()

        self.assertIn("1189584062", repo.load_used_ozon_product_ids())
        self.assertIn("898078910803", repo.load_used_supplier_offer_ids())

    def test_seed_identity_is_stable_across_legacy_and_current_pool_ids(self) -> None:
        repo = FsRepo(self.context)
        legacy = SeedProduct(
            seed_id="seed-1440",
            title_or_keyword="legacy title",
            product_clue="legacy clue",
        )
        current = SeedProduct(
            seed_id="seed-5000-1440",
            title_or_keyword="current title",
            product_clue="current clue",
        )

        self.assertEqual(repo.seed_identity_key(legacy), repo.seed_identity_key(current))

    def test_used_seed_ledger_is_idempotent_by_stable_identity(self) -> None:
        repo = FsRepo(self.context)
        legacy = SeedProduct(
            seed_id="seed-1440",
            title_or_keyword="legacy title",
            product_clue="legacy clue",
        )
        current = SeedProduct(
            seed_id="seed-5000-1440",
            title_or_keyword="current title",
            product_clue="current clue",
        )

        repo.append_used_seeds("wb-first", [legacy], "workbench_sampled")
        repo.append_used_seeds("wb-second", [current], "workbench_sampled")

        rows = [json.loads(line) for line in repo.used_seed_path.read_text(encoding="utf-8").splitlines() if line]
        self.assertEqual(1, len(rows))
        self.assertEqual(repo.seed_identity_key(current), rows[0]["seed_identity_key"])
        self.assertEqual({repo.seed_identity_key(current)}, repo.load_used_seed_identity_keys())

    def test_concurrent_browser_bridge_status_writes_remain_valid_json(self) -> None:
        repo = FsRepo(self.context)

        def write_status(index: int) -> None:
            repo.save_browser_bridge_status(
                {
                    "source": f"writer-{index}",
                    "message": "x" * (40 + index * 17),
                    "extension_version": "0.1.13",
                }
            )

        with ThreadPoolExecutor(max_workers=12) as executor:
            list(executor.map(write_status, range(60)))

        parsed = json.loads(repo.browser_bridge_status_path.read_text(encoding="utf-8"))
        self.assertEqual("0.1.13", parsed["extension_version"])

    def test_initialize_runtime_creates_active_seed_pool_from_bundled_asset(self) -> None:
        repo = FsRepo(self.context)

        repo.initialize_runtime()

        self.assertTrue((self.context.runtime_root / "config" / "seed_pool.initial.json").exists())
        self.assertTrue((self.context.runtime_root / "state" / "seed_pool.active.json").exists())
        self.assertTrue((self.context.runtime_root / "state" / "seed_pool.used.jsonl").exists())
        self.assertEqual(5000, len(repo.load_active_seeds()))

    def test_initialize_runtime_replaces_old_package_without_rehydrating_used_seed(self) -> None:
        repo = FsRepo(self.context)
        bundled = json.loads(self.context.paths.initial_seed_json.read_text(encoding="utf-8"))
        used_seed_id = bundled["seeds"][0]["seed_id"]
        repo.config_dir.mkdir(parents=True)
        repo.state_dir.mkdir(parents=True)
        repo.runs_dir.mkdir(parents=True)
        repo.config_initial_seed_path.write_text(
            json.dumps({"package_version": "seed_pool.refined.2000.v1", "seeds": []}),
            encoding="utf-8",
        )
        repo.active_seed_path.write_text(
            json.dumps(
                {
                    "package_version": "seed_pool.refined.2000.v1",
                    "seeds": [{"seed_id": "seed-legacy", "title_or_keyword": "legacy", "product_clue": "legacy"}],
                }
            ),
            encoding="utf-8",
        )
        repo.used_seed_path.write_text(
            json.dumps({"seed_id": used_seed_id}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        repo.initialize_runtime()

        active_payload = json.loads(repo.active_seed_path.read_text(encoding="utf-8"))
        config_payload = json.loads(repo.config_initial_seed_path.read_text(encoding="utf-8"))
        active_ids = {item["seed_id"] for item in active_payload["seeds"]}
        self.assertEqual("seed_pool.refined.5000.v1", active_payload["package_version"])
        self.assertEqual("seed_pool.refined.5000.v1", config_payload["package_version"])
        self.assertEqual(4999, len(active_ids))
        self.assertNotIn(used_seed_id, active_ids)
        self.assertNotIn("seed-legacy", active_ids)

    def test_initialize_runtime_does_not_restore_removed_used_seed(self) -> None:
        repo = FsRepo(self.context)
        repo.initialize_runtime()
        seeds = repo.load_active_seeds()
        removed_seed = seeds[0]
        repo.save_active_seeds(seeds[1:])
        repo.append_used_seeds("run-test", [removed_seed], "sampled")

        repo.initialize_runtime()

        active_ids = {seed.seed_id for seed in repo.load_active_seeds()}
        self.assertNotIn(removed_seed.seed_id, active_ids)
        self.assertIn(removed_seed.seed_id, repo.load_used_seed_ids())
