from __future__ import annotations

from ozon_v2.services.run_service import RunService


def ozon_v2_doctor() -> dict:
    return RunService().doctor().to_dict()

