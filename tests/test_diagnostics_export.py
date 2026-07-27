from __future__ import annotations

from io import BytesIO
import json
from zipfile import ZipFile

from ozon_v2.adapters.fs_repo import FsRepo
from ozon_v2.services.diagnostics_export_service import DiagnosticsExportService

from tests.helpers import RuntimeTestCase


class DiagnosticsExportTests(RuntimeTestCase):
    def test_current_batch_zip_contains_required_files_and_redacts_secrets(self) -> None:
        repo = FsRepo(self.context)
        repo.initialize_runtime()
        run = repo.create_workbench_batch_record(target_count=5)
        run_id = run["run_id"]
        secrets = {
            "api_key": "api-secret-123",
            "cookie": "session-cookie-456",
            "authorization": "Bearer auth-secret-789",
            "proxy_password": "proxy-secret-012",
        }
        repo.append_run_event(
            run_id,
            "runner.failed",
            "Request failed with sensitive diagnostic context.",
            dict(secrets),
        )
        bridge = {
            "stage": "task_failed",
            "details": {"Cookie": secrets["cookie"], "url": "https://www.ozon.ru/product/test/"},
        }
        runner = {"state": "failed", "message": secrets["authorization"]}

        archive = DiagnosticsExportService(repo).build_current_batch_zip(
            run_id,
            runner_status=runner,
            browser_bridge=bridge,
        )

        self.assertEqual(f"ozon-v2-diagnostics-{run_id}.zip", archive.filename)
        with ZipFile(BytesIO(archive.content)) as bundle:
            self.assertEqual(
                {
                    "summary.json",
                    "run.json",
                    "events.json",
                    "runner.json",
                    "browser_bridge.json",
                    "errors.json",
                    "files.json",
                },
                set(bundle.namelist()),
            )
            extracted = "\n".join(bundle.read(name).decode("utf-8") for name in bundle.namelist())
        for secret in secrets.values():
            self.assertNotIn(secret, extracted)
        self.assertIn("[REDACTED]", extracted)
        summary = json.loads(ZipFile(BytesIO(archive.content)).read("summary.json"))
        self.assertEqual(run_id, summary["run_id"])
        self.assertEqual("current_batch", summary["scope"])
