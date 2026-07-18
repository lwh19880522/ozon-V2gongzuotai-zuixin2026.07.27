from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json

from ozon_v2.adapters.fs_repo import FsRepo

from tests.helpers import RuntimeTestCase


class FsRepoTests(RuntimeTestCase):
    def test_clear_workbench_batches_only_removes_verified_workbench_runs(self) -> None:
        repo = FsRepo(self.context)
        repo.initialize_runtime()
        first = repo.create_workbench_batch_record(target_count=1)
        second = repo.create_workbench_batch_record(target_count=2)
        legacy_dir = repo.run_dir("run-legacy-keep")
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "run.json").write_text(
            json.dumps({"run_id": "run-legacy-keep", "status": "finalized"}),
            encoding="utf-8",
        )
        unrelated_dir = repo.runs_dir / "manual-notes"
        unrelated_dir.mkdir()

        credentials_before = repo.credentials_template_path.read_bytes()
        seeds_before = repo.active_seed_path.read_bytes()
        dedupe_before = repo.existing_store_dedupe_path.read_bytes()

        result = repo.clear_workbench_batches()

        self.assertEqual({first["run_id"], second["run_id"]}, set(result["deleted_run_ids"]))
        self.assertFalse(repo.run_dir(first["run_id"]).exists())
        self.assertFalse(repo.run_dir(second["run_id"]).exists())
        self.assertTrue(legacy_dir.exists())
        self.assertTrue(unrelated_dir.exists())
        self.assertEqual(credentials_before, repo.credentials_template_path.read_bytes())
        self.assertEqual(seeds_before, repo.active_seed_path.read_bytes())
        self.assertEqual(dedupe_before, repo.existing_store_dedupe_path.read_bytes())

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
        self.assertEqual(2000, len(repo.load_active_seeds()))

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
