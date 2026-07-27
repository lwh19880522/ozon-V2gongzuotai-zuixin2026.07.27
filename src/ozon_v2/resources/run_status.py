from __future__ import annotations

from ozon_v2.services.run_service import RunService


def get_run_status_resource(run_id: str) -> dict:
    return RunService().status(run_id=run_id).to_dict()

