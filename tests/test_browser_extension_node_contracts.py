from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


NODE_CONTRACTS = (
    "test_browser_collection_progress.js",
    "test_browser_content_recovery.js",
    "test_browser_content_timeout_recovery.js",
    "test_browser_extension_background.js",
    "test_browser_extension_managed_supplier_round.js",
    "test_browser_extension_supplier.js",
    "test_browser_product_evidence.js",
    "test_browser_seller_evidence.js",
    "test_browser_workbench_content.js",
    "test_supplier_content_script.js",
    "test_supplier_panel_lifecycle.js",
    "test_supplier_same_tab_navigation.js",
    "test_supplier_selection_content_script.js",
    "test_supplier_selection_fixed_axis_sku.js",
    "test_supplier_selection_image_upload.js",
    "test_supplier_specification_table_sku.js",
    "test_supplier_selection_without_sku_matrix.js",
)


@pytest.mark.parametrize("script_name", NODE_CONTRACTS)
def test_browser_extension_node_contract(script_name: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for browser-extension contracts")
    script = Path(__file__).with_name(script_name)
    completed = subprocess.run(
        [node, str(script)],
        cwd=script.parent.parent,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, (completed.stdout or "") + (completed.stderr or "")
