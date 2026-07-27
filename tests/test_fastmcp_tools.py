from __future__ import annotations

from unittest import TestCase


class FastMcpToolTests(TestCase):
    def test_server_registers_thin_tools(self) -> None:
        from mcp.server import create_app

        app = create_app()

        self.assertTrue(hasattr(app, "tool"))
        if hasattr(app, "tools"):
            self.assertIn("ozon_v2_doctor", app.tools)
            self.assertIn("ozon_v2_credentials_template", app.tools)
            self.assertIn("ozon_v2_credentials_status", app.tools)
            self.assertIn("ozon_v2_open_credential_assistant", app.tools)
            self.assertIn("ozon_v2_save_credentials", app.tools)
            self.assertIn("ozon_v2_refresh_existing_store_dedupe", app.tools)
            self.assertIn("ozon_v2_start_run", app.tools)
            self.assertIn("ozon_v2_replace_sampled_seed", app.tools)
            self.assertIn("ozon_v2_ingest_collection_output", app.tools)
            self.assertIn("ozon_v2_workbench_start_batch", app.tools)
            self.assertIn("ozon_v2_workbench_allowed_actions", app.tools)
            self.assertIn("ozon_v2_workbench_dispatch", app.tools)
