from __future__ import annotations

from ozon_v2.services.collection_contract_service import CollectionContractService
from ozon_v2.services.run_service import RunService


def ozon_v2_start_run(target_count: int, random_seed: int | None = None) -> dict:
    return RunService().start_run(target_count=target_count, random_seed=random_seed).to_dict()


def ozon_v2_replace_sampled_seed(
    run_id: str,
    rejected_seed_id: str,
    reason: str,
    random_seed: int | None = None,
) -> dict:
    return RunService().replace_sampled_seed(
        run_id=run_id,
        rejected_seed_id=rejected_seed_id,
        reason=reason,
        random_seed=random_seed,
    ).to_dict()


def ozon_v2_status(run_id: str | None = None) -> dict:
    return RunService().status(run_id=run_id).to_dict()


def ozon_v2_next(run_id: str) -> dict:
    next_result = RunService().next_action(run_id)
    if not next_result.ok or next_result.code != "next.ozon_collection_contract":
        return next_result.to_dict()
    return CollectionContractService().build_ozon_collection_contract(run_id).to_dict()
