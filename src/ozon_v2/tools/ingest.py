from __future__ import annotations

from typing import Any

from ozon_v2.services.run_service import RunService


def ozon_v2_ingest_collection_output(run_id: str, collection_output: dict[str, Any]) -> dict:
    return RunService().ingest_collection_output(run_id=run_id, payload=collection_output).to_dict()

