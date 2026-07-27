from __future__ import annotations

from ozon_v2.services.workbench_service import WorkbenchService


def ozon_v2_workbench_start_batch(target_count: int) -> dict:
    return WorkbenchService().start_batch(target_count=target_count).to_dict()


def ozon_v2_workbench_allowed_actions(run_id: str) -> dict:
    return WorkbenchService().allowed_actions(run_id=run_id).to_dict()


def ozon_v2_workbench_dispatch(run_id: str, action: str) -> dict:
    return WorkbenchService().dispatch(run_id=run_id, action=action).to_dict()
