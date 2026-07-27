from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

from ozon_v2.adapters.fs_repo import FsRepo
from ozon_v2.adapters.supplier_browser_worker import SupplierBrowserWorker

from tests.helpers import RuntimeTestCase


class SupplierBrowserWorkerTests(RuntimeTestCase):
    def test_worker_runs_saved_links_with_direct_network_contract(self) -> None:
        repo = FsRepo(self.context)
        run = repo.create_workbench_batch_record(target_count=1)
        run_id = run["run_id"]
        repo.save_supplier_collection_contract(
            run_id,
            {
                "run_id": run_id,
                "network": {"mode": "direct", "proxy_disabled": True},
                "items": [
                    {
                        "seed_id": "seed-test",
                        "supplier_url": "https://detail.1688.com/offer/123456789012.html",
                        "user_verified_exact_match": True,
                    }
                ],
            },
        )
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(command)
            input_payload = json.loads((repo.run_dir(run_id) / "artifacts" / "supplier_worker" / "input.json").read_text(encoding="utf-8"))
            output_path = repo.run_dir(run_id) / "artifacts" / "supplier_worker" / "output.json"
            output_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "payload": {
                            "run_id": run_id,
                            "worker": "local_playwright_direct",
                            "source": "1688_user_verified_link",
                            "network": input_payload["network"],
                            "supplier_products": [{"seed_id": "seed-test"}],
                        },
                    }
                ),
                encoding="utf-8",
            )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        worker = SupplierBrowserWorker(repo, subprocess_runner=fake_run)

        result = worker.collect(run_id)

        self.assertTrue(result.ok)
        self.assertEqual("supplier_worker.collected", result.code)
        self.assertEqual("node", calls[0][0])
        self.assertTrue(calls[0][1].endswith("collect_1688_supplier_link.js"))
        worker_input = json.loads((repo.run_dir(run_id) / "artifacts" / "supplier_worker" / "input.json").read_text(encoding="utf-8"))
        self.assertEqual({"mode": "direct", "proxy_disabled": True}, worker_input["network"])
        self.assertEqual("https://detail.1688.com/offer/123456789012.html", worker_input["items"][0]["supplier_url"])

    def test_playwright_script_disables_proxy_and_collects_required_public_fields(self) -> None:
        script = (self.project_root / "scripts" / "collect_1688_supplier_link.js").read_text(encoding="utf-8")

        self.assertIn('"--no-proxy-server"', script)
        self.assertIn("supplier_products", script)
        self.assertIn("domestic_shipping_evidence", script)
        self.assertIn("selected_options", script)
        self.assertIn("images", script)

    def test_playwright_script_accepts_standard_detail_url_with_tracking_query(self) -> None:
        script_path = self.project_root / "scripts" / "collect_1688_supplier_link.js"
        valid_url = (
            "https://detail.1688.com/offer/1058407451332.html"
            "?spm=a26352.b28411319/2508.0.0&cosite=-&tracelog=p4p"
        )
        invalid_url = "https://detail.1688.com.evil.example/offer/1058407451332.html"
        code = (
            f"const worker=require({json.dumps(str(script_path))});"
            f"process.stdout.write(JSON.stringify([worker.isValid1688DetailUrl({json.dumps(valid_url)}),"
            f"worker.isValid1688DetailUrl({json.dumps(invalid_url)})]));"
        )

        completed = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=20)

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual([True, False], json.loads(completed.stdout))
