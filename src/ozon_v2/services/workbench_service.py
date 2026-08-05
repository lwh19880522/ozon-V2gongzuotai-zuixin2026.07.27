from __future__ import annotations

import json
import hashlib
import ipaddress
import os
import random
import re
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

from ozon_v2.adapters.fs_repo import FsRepo
from ozon_v2.adapters.seller_api import (
    SellerApiAdapter,
    SellerApiError,
    assess_category_template_match,
)
from ozon_v2.app.result import Result
from ozon_v2.domain.models import OzonCandidate, QueryGenerationStatus, SeedProduct, SeedSearchQuery, WorkbenchAction, WorkbenchState, utc_now_iso
from ozon_v2.domain.policies import decide_ozon_candidate_dedupe, decide_seed_existing_product_dedupe, seed_has_generated_ozon_query
from ozon_v2.domain.pricing import (
    PricingInput,
    PricingPolicy,
    calculate_listing_price,
    round_up_to_dot_90,
)
from ozon_v2.domain.supplier_sku import (
    SupplierSkuOption,
    SupplierSkuSelectionReceipt,
    documented_composition_quantity,
    validate_supplier_sku_option,
)
from ozon_v2.domain.state_machine import allowed_workbench_actions, transition_workbench_state
from ozon_v2.domain.validators import validate_attribute_template_result, validate_ozon_collection_result, validate_seed_ready_for_ozon
from ozon_v2.images.contracts import SubjectMasterSelection
from ozon_v2.images.queue import ImageGenerationQueue, ImageRepairRequestError
from ozon_v2.images.worker import is_exact_three_by_four_image
from ozon_v2.services.attribute_mapping_service import (
    attribute_content_score_progress,
    canonical_attribute_label,
    is_visual_inference_field,
    map_template_attributes,
    normalize_attribute_label,
)
from ozon_v2.services.collection_contract_service import (
    CREATIVE_FIELDS_REQUIRING_REWRITE,
    CollectionContractService,
    OBJECTIVE_ATTRIBUTE_PREFILL_FIELDS,
)
from ozon_v2.services.credential_service import CredentialService
from ozon_v2.services.seed_query_service import SeedQueryService
from ozon_v2.services.seller_history_service import SellerHistoryService


_CONTENT_RESOLUTION_CLASSES = {
    "source_fact_missing",
    "supplier_identity_missing",
    "dictionary_value_missing",
    "evidence_conflict",
    "not_applicable",
}
_RUSSIAN_OBJECTIVE_FIELDS = {
    "model",
    "type",
    "package_contents",
    "color",
    "material",
    "country",
    "gender",
}
_PRE_UPLOAD_COMPLIANCE_DECISION_LABELS = frozenset(
    {
        "нужен код маркировки",
        "требуется код маркировки",
        "подпись 18",
        "признак 18",
        "знак 18",
        "маркировка 18",
        "нужна подпись 18",
    }
)
_INTERNAL_LIFECYCLE_ACTIONS = frozenset(
    action for action in WorkbenchAction if action.value.startswith("mark_")
)
_REPLACEMENT_RECOVERY_STATES = frozenset(
    {
        WorkbenchState.OZON_COLLECTED,
        WorkbenchState.SUPPLIER_REVIEW,
        WorkbenchState.NEEDS_MANUAL_REVIEW,
        WorkbenchState.FAILED_RETRYABLE,
        WorkbenchState.FAILED_BLOCKED,
    }
)


class WorkbenchService:
    def __init__(
        self,
        repo: FsRepo | None = None,
        credential_service: CredentialService | None = None,
        seller_history_service: SellerHistoryService | None = None,
        seed_query_service: SeedQueryService | None = None,
        collection_contract_service: CollectionContractService | None = None,
        seller_api_adapter: SellerApiAdapter | None = None,
        supplier_image_downloader: Callable[[str, Path], Path] | None = None,
    ) -> None:
        self.repo = repo or FsRepo()
        self.credential_service = credential_service or CredentialService(self.repo)
        self.seller_history_service = seller_history_service or SellerHistoryService(self.repo)
        self.seed_query_service = seed_query_service or SeedQueryService()
        self.collection_contract_service = collection_contract_service or CollectionContractService(self.repo)
        self.seller_api_adapter = seller_api_adapter or SellerApiAdapter(self.repo)
        self.supplier_image_downloader = supplier_image_downloader or self._download_supplier_image
        self._upload_state_lock = threading.RLock()
        self._run_mutation_locks_guard = threading.Lock()
        self._run_mutation_locks: dict[str, threading.RLock] = {}

    def _run_mutation_lock(self, run_id: str) -> threading.RLock:
        with self._run_mutation_locks_guard:
            return self._run_mutation_locks.setdefault(run_id, threading.RLock())

    @staticmethod
    def _browser_dispatch_token(run: dict[str, Any]) -> str:
        return str(run.get("browser_task_resumed_at") or run.get("created_at") or "").strip()

    def _reject_stale_stage_result(
        self,
        run_id: str,
        stage: str,
        run: dict[str, Any],
        errors: list[str],
    ) -> Result:
        event = self.repo.append_run_event(
            run_id,
            f"{stage}.ingest_stale",
            "Browser result was rejected because its collection contract is no longer current.",
            {"errors": errors},
        )
        return Result.failure(
            f"workbench.{stage}_stale_result",
            "The browser result belongs to an outdated collection contract and was not saved.",
            errors=errors,
            data=self._response_payload(run, event),
        )

    def _stage_snapshot_errors(
        self,
        *,
        run: dict[str, Any],
        expected_state: WorkbenchState,
        expected_seed_ids: list[str],
        current_seed_ids: list[str],
        expected_dispatch_token: str,
        payload_dispatch_token: str,
    ) -> list[str]:
        errors: list[str] = []
        if WorkbenchState(run["status"]) != expected_state:
            errors.append(
                f"batch status changed from {expected_state.value} to {run['status']}"
            )
        if current_seed_ids != expected_seed_ids:
            errors.append("sampled seed ids changed while the browser result was being processed")
        current_dispatch_token = self._browser_dispatch_token(run)
        if current_dispatch_token != expected_dispatch_token:
            errors.append("browser dispatch token changed while the result was being processed")
        if payload_dispatch_token and payload_dispatch_token != current_dispatch_token:
            errors.append("browser result dispatch token is stale")
        return errors

    def start_batch(self, target_count: int) -> Result:
        if target_count <= 0:
            return Result.failure("workbench.invalid_target_count", "target_count must be greater than zero.")
        self.repo.initialize_runtime()
        run = self.repo.create_workbench_batch_record(target_count)
        event = self.repo.append_run_event(
            run["run_id"],
            "workbench.batch_created",
            "Workbench batch created with publish locked.",
            {"target_count": target_count, "publish_locked": True},
        )
        return Result.success(
            "workbench.batch_created",
            "Workbench batch created.",
            {
                "run": run,
                "allowed_actions": self._allowed_action_values(run),
                "gates": self._gate_status(),
                "last_event": event.to_dict(),
            },
        )

    def clear_all_batches(self, confirmed: bool = False) -> Result:
        if confirmed is not True:
            return Result.failure(
                "workbench.clear_confirmation_required",
                "Explicit confirmation is required before clearing all workbench batches.",
            )
        archived_seed_usage = self.repo.archive_recognized_batch_seed_usage()
        cleared = self.repo.clear_workbench_batches()
        bridge = self.repo.clear_browser_bridge_task_status()
        return Result.success(
            "workbench.batches_cleared",
            "All current and historical workbench batches were cleared.",
            {
                **cleared,
                **archived_seed_usage,
                "preserved": [
                    "store_authorization",
                    "existing_store_dedupe",
                    "seed_pool",
                    "project_code",
                ],
                "browser_bridge": bridge,
            },
        )

    def allowed_actions(self, run_id: str) -> Result:
        try:
            run = self.recover_browser_task_state(run_id)
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
            return Result.failure(
                "workbench.batch_not_found",
                "The requested workbench batch no longer exists.",
                data={"run_id": run_id},
            )
        return Result.success(
            "workbench.allowed_actions",
            "Allowed actions for current workbench state.",
            {
                "run_id": run_id,
                "run": run,
                "status": run["status"],
                "publish_locked": run.get("publish_locked", True),
                "allowed_actions": self._allowed_action_values(run),
                "gates": self._gate_status(),
                "progress": self._run_progress(run),
            },
        )

    def replace_exhausted_attribute_template_seed(
        self,
        run_id: str,
        rejected_seed_id: str,
        reason: str,
        random_seed: int | None = None,
    ) -> Result:
        with self._run_mutation_lock(run_id):
            return self._replace_exhausted_attribute_template_seed(
                run_id,
                rejected_seed_id,
                reason,
                random_seed=random_seed,
            )

    def _replace_exhausted_attribute_template_seed(
        self,
        run_id: str,
        rejected_seed_id: str,
        reason: str,
        random_seed: int | None = None,
    ) -> Result:
        run = self.repo.load_run(run_id)
        current_state = WorkbenchState(run["status"])
        if current_state not in {
            WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTING,
            WorkbenchState.OZON_COLLECTING,
        }:
            return Result.failure(
                "workbench.exhausted_seed_invalid_state",
                "An exhausted Ozon seed can only be replaced during template or product collection.",
                data={"run_id": run_id, "status": run["status"]},
            )
        sampled = self.repo.load_sampled_seeds(run_id)
        rejected = next((seed for seed in sampled if seed.seed_id == rejected_seed_id), None)
        if rejected is None:
            return Result.failure(
                "workbench.exhausted_seed_not_sampled",
                "The exhausted seed no longer belongs to this batch.",
                data={"run_id": run_id, "rejected_seed_id": rejected_seed_id},
            )

        eligible = self._eligible_replacement_seeds(run_id, sampled)
        if not eligible:
            return Result.failure(
                "workbench.exhausted_seed_no_replacement",
                "No eligible replacement seed remains after store dedupe filtering.",
                data={"run_id": run_id, "rejected_seed_id": rejected_seed_id},
            )

        selected_random_seed = random_seed if random_seed is not None else random.SystemRandom().randint(1, 2**31 - 1)
        replacement = self.repo.sample_seeds(eligible, 1, selected_random_seed)[0]
        self._ensure_candidate_slots(run, sampled)
        replacement_index = next(index for index, seed in enumerate(sampled) if seed.seed_id == rejected_seed_id)
        sampled[replacement_index] = replacement
        self.repo.save_sampled_seeds(run_id, sampled)
        self.repo.append_used_seeds(run_id, [replacement], "workbench_replacement_sampled")
        self.repo.append_rejected_seed_attempt(run_id, rejected, reason, replacement.seed_id)

        run["sampled_seed_ids"] = [seed.seed_id for seed in sampled]
        run["rejected_seed_ids"] = [item.get("seed_id") for item in self.repo.load_rejected_seed_attempts(run_id)]
        run["replacement_pending_seed_ids"] = [replacement.seed_id]
        run["status"] = WorkbenchState.SEED_SELECTED.value
        run["attribute_template_contract_ready"] = False
        run["attribute_template_collected"] = False
        run["ozon_collection_contract_ready"] = False
        run["ozon_collected"] = False
        for key in ("attribute_template_contract_path", "ozon_collection_contract_path"):
            run.pop(key, None)
        run["browser_task_cancelled"] = False
        run["browser_task_resumed_at"] = utc_now_iso()
        self._update_query_summary(run, sampled)
        self.repo.save_run(run)
        event = self.repo.append_run_event(
            run_id,
            "seed_sampling.replaced_after_exhaustion",
            "An exhausted Ozon seed was recorded and replaced without removing the active seed pool.",
            {
                "rejected_seed_id": rejected_seed_id,
                "replacement_seed_id": replacement.seed_id,
                "reason": reason,
                "random_seed": selected_random_seed,
            },
        )
        return Result.success(
            "workbench.exhausted_seed_replaced",
            "The exhausted Ozon seed was replaced and the browser contract will be regenerated.",
            self._response_payload(
                run,
                event,
                {"rejected_seed": rejected.to_dict(), "replacement_seed": replacement.to_dict()},
            ),
        )

    def _eligible_replacement_seeds(
        self,
        run_id: str,
        sampled: list[SeedProduct],
    ) -> list[SeedProduct]:
        rejected_attempts = self.repo.load_rejected_seed_attempts(run_id)
        excluded_ids = (
            self.repo.load_used_seed_ids()
            | self.repo.load_blacklisted_seed_ids()
            | {str(item.get("seed_id") or "") for item in rejected_attempts}
            | {seed.seed_id for seed in sampled}
        )
        excluded_identity_keys = (
            self.repo.load_used_seed_identity_keys()
            | self.repo.load_blacklisted_seed_identity_keys()
            | {self.repo.seed_identity_key(seed) for seed in sampled}
            | {
                self.repo.seed_identity_key(
                    item.get("seed") or str(item.get("seed_id") or "")
                )
                for item in rejected_attempts
                if item.get("seed") or item.get("seed_id")
            }
        )
        existing_products = self.repo.load_existing_products()
        eligible: list[SeedProduct] = []
        for seed in self.repo.load_active_seeds():
            if seed.seed_id in excluded_ids:
                continue
            if self.repo.seed_identity_key(seed) in excluded_identity_keys:
                continue
            decision = decide_seed_existing_product_dedupe(seed, existing_products)
            if decision.kind.value == "clear":
                eligible.append(seed)
        return eligible

    def ingest_attribute_template_result(self, run_id: str, payload: dict[str, Any]) -> Result:
        run = self.repo.load_run(run_id)
        if WorkbenchState(run["status"]) != WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTING:
            event = self.repo.append_run_event(
                run_id,
                "attribute_template.ingest_rejected",
                "Attribute template result was rejected because the batch is not waiting for template collection.",
                {"status": run["status"]},
            )
            return Result.failure(
                "workbench.attribute_template_not_expected",
                "Attribute template result is only accepted while status is attribute_template_collecting.",
                data=self._response_payload(run, event),
            )
        dispatch_token = self._browser_dispatch_token(run)
        payload_dispatch_token = str(payload.get("dispatch_token") or "").strip()
        if payload_dispatch_token and payload_dispatch_token != dispatch_token:
            return self._reject_stale_stage_result(
                run_id,
                "attribute_template",
                run,
                ["browser result dispatch token is stale"],
            )
        seeds = self._safe_load_sampled_seeds(run_id)
        all_seed_ids = [seed.seed_id for seed in seeds]
        expected_seed_ids = self._pending_or_all_seed_ids(
            run,
            all_seed_ids,
            result_kind="attribute_template",
        )
        template_items = [
            item
            for item in payload.get("seed_templates", [])
            if isinstance(item, dict)
        ]
        full_current_snapshot = self._is_full_current_seed_snapshot(
            template_items,
            all_seed_ids,
        )
        if full_current_snapshot:
            expected_seed_ids = all_seed_ids
        errors = validate_attribute_template_result(payload, expected_seed_ids)
        if errors:
            event = self.repo.append_run_event(
                run_id,
                "attribute_template.ingest_invalid",
                "Attribute template result failed validation.",
                {"errors": errors},
            )
            return Result.failure(
                "workbench.attribute_template_invalid",
                "Attribute template result failed validation.",
                errors=errors,
                data=self._response_payload(run, event),
            )
        binding_errors = self._attribute_template_product_binding_errors(
            run_id,
            payload,
            expected_seed_ids,
        )
        if binding_errors:
            event = self.repo.append_run_event(
                run_id,
                "attribute_template.product_binding_mismatch",
                "Attribute template evidence did not belong to the final locked Ozon product revision.",
                {"errors": binding_errors},
            )
            return Result.failure(
                "workbench.attribute_template_product_mismatch",
                "Attribute template evidence must match the exact locked Ozon product, slot, and revision.",
                errors=binding_errors,
                data=self._response_payload(run, event),
            )
        try:
            payload = self._attach_seller_attribute_templates(payload)
        except SellerApiError as exc:
            event = self.repo.append_run_event(
                run_id,
                "attribute_template.seller_schema_unavailable",
                "Seller category attribute template is required before the template gate can pass.",
                {"error": str(exc)},
            )
            return Result.failure(
                "workbench.seller_attribute_template_unavailable",
                "Seller category attribute template is required before Ozon collection.",
                errors=[str(exc)],
                data=self._response_payload(run, event),
            )
        seller_schema_errors = validate_attribute_template_result(payload, expected_seed_ids, require_seller_schema=True)
        if seller_schema_errors:
            event = self.repo.append_run_event(
                run_id,
                "attribute_template.seller_schema_invalid",
                "Seller category attribute template failed validation.",
                {"errors": seller_schema_errors},
            )
            return Result.failure(
                "workbench.seller_attribute_template_invalid",
                "Seller category attribute template failed validation.",
                errors=seller_schema_errors,
                data=self._response_payload(run, event),
            )
        subject_errors = self._attribute_template_subject_errors(
            run_id,
            payload,
            expected_seed_ids,
        )
        if subject_errors:
            mismatch_seed_ids = [
                seed_id
                for seed_id in expected_seed_ids
                if any(
                    error.startswith(f"seed {seed_id} ")
                    for error in subject_errors
                )
            ]
            return self._retire_category_subject_mismatches(
                run_id,
                payload,
                subject_errors=subject_errors,
                mismatch_seed_ids=mismatch_seed_ids,
                all_seed_ids=all_seed_ids,
                dispatch_token=dispatch_token,
                payload_dispatch_token=payload_dispatch_token,
            )
        with self._run_mutation_lock(run_id):
            current_run = self.repo.load_run(run_id)
            current_seed_ids = [
                seed.seed_id for seed in self._safe_load_sampled_seeds(run_id)
            ]
            stale_errors = self._stage_snapshot_errors(
                run=current_run,
                expected_state=WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTING,
                expected_seed_ids=all_seed_ids,
                current_seed_ids=current_seed_ids,
                expected_dispatch_token=dispatch_token,
                payload_dispatch_token=payload_dispatch_token,
            )
            if stale_errors:
                return self._reject_stale_stage_result(
                    run_id,
                    "attribute_template",
                    current_run,
                    stale_errors,
                )
            run = current_run
            if run.get("replacement_pending_seed_ids") and not full_current_snapshot:
                try:
                    existing_payload = self.repo.load_attribute_template_result(run_id)
                except FileNotFoundError:
                    existing_payload = {}
                pending_ids = set(expected_seed_ids)
                retained_templates = [
                    item
                    for item in existing_payload.get("seed_templates", [])
                    if isinstance(item, dict) and str(item.get("seed_id") or "") not in pending_ids
                ]
                payload = {**payload, "seed_templates": retained_templates + list(payload.get("seed_templates", []))}
                merged_errors = validate_attribute_template_result(payload, all_seed_ids, require_seller_schema=True)
                merged_errors.extend(
                    self._attribute_template_product_binding_errors(
                        run_id,
                        payload,
                        all_seed_ids,
                    )
                )
                if merged_errors:
                    event = self.repo.append_run_event(
                        run_id,
                        "attribute_template.merge_invalid",
                        "Replacement attribute template could not be merged with retained evidence.",
                        {"errors": merged_errors},
                    )
                    return Result.failure(
                        "workbench.attribute_template_merge_invalid",
                        "Replacement attribute template could not be merged with retained evidence.",
                        errors=merged_errors,
                        data=self._response_payload(run, event),
                    )
            result_path = self.repo.save_attribute_template_result(run_id, payload)
            current = WorkbenchState(run["status"])
            run["status"] = transition_workbench_state(current, WorkbenchAction.MARK_ATTRIBUTE_TEMPLATE_COLLECTED).value
            run["attribute_template_collected"] = True
            run["attribute_template_result_path"] = str(result_path)
            ozon_payload = self.repo.load_ozon_collection_result(run_id)
            review = self._build_supplier_review(run_id, ozon_payload)
            review_path = self.repo.save_supplier_review(run_id, review)
            run["supplier_review_path"] = str(review_path)
            run["status"] = transition_workbench_state(
                WorkbenchState(run["status"]),
                WorkbenchAction.OPEN_SUPPLIER_REVIEW,
            ).value
            run.pop("replacement_pending_seed_ids", None)
            self.repo.save_run(run)
            event = self.repo.append_run_event(
                run_id,
                "attribute_template.ingested",
                "Attribute template result was bound to final Ozon products and supplier review was opened.",
                {
                    "result_path": str(result_path),
                    "review_path": str(review_path),
                    "seed_count": len(expected_seed_ids),
                },
            )
            return Result.success(
                "workbench.attribute_template_ingested",
                "Final-product category templates are complete and supplier review is open.",
                self._response_payload(run, event),
            )

    def _retire_category_subject_mismatches(
        self,
        run_id: str,
        payload: dict[str, Any],
        *,
        subject_errors: list[str],
        mismatch_seed_ids: list[str],
        all_seed_ids: list[str],
        dispatch_token: str,
        payload_dispatch_token: str,
    ) -> Result:
        if not mismatch_seed_ids:
            run = self.repo.load_run(run_id)
            event = self.repo.append_run_event(
                run_id,
                "attribute_template.category_subject_mismatch_unbound",
                "Category-subject mismatch could not be bound to an active candidate slot.",
                {"errors": subject_errors},
            )
            return Result.failure(
                "workbench.category_subject_mismatch_unbound",
                "The contradictory category evidence could not be retired safely.",
                errors=subject_errors,
                data=self._response_payload(run, event),
            )

        with self._run_mutation_lock(run_id):
            run = self.repo.load_run(run_id)
            current_seed_ids = [
                seed.seed_id for seed in self._safe_load_sampled_seeds(run_id)
            ]
            stale_errors = self._stage_snapshot_errors(
                run=run,
                expected_state=WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTING,
                expected_seed_ids=all_seed_ids,
                current_seed_ids=current_seed_ids,
                expected_dispatch_token=dispatch_token,
                payload_dispatch_token=payload_dispatch_token,
            )
            if stale_errors:
                return self._reject_stale_stage_result(
                    run_id,
                    "attribute_template",
                    run,
                    stale_errors,
                )

            ozon_payload = self.repo.load_ozon_collection_result(run_id)
            candidates = {
                str(item.get("seed_id") or ""): item
                for item in ozon_payload.get("ozon_candidates", [])
                if isinstance(item, dict) and item.get("seed_id")
            }
            missing_candidates = [
                seed_id
                for seed_id in mismatch_seed_ids
                if not str(
                    (candidates.get(seed_id) or {}).get("ozon_product_id") or ""
                ).strip()
            ]
            if missing_candidates:
                event = self.repo.append_run_event(
                    run_id,
                    "attribute_template.category_subject_mismatch_unbound",
                    "Category-subject mismatch referenced a slot without a locked Ozon product.",
                    {
                        "errors": subject_errors,
                        "missing_seed_ids": missing_candidates,
                    },
                )
                return Result.failure(
                    "workbench.category_subject_mismatch_unbound",
                    "The contradictory category evidence could not be retired safely.",
                    errors=subject_errors,
                    data=self._response_payload(run, event),
                )

            reason_by_seed = {
                seed_id: next(
                    (
                        error
                        for error in subject_errors
                        if error.startswith(f"seed {seed_id} ")
                    ),
                    "The locked Ozon product contradicts its category subject.",
                )
                for seed_id in mismatch_seed_ids
            }
            sampled = self._safe_load_sampled_seeds(run_id)
            sampled_by_seed = {seed.seed_id: seed for seed in sampled}
            slots_by_seed = self._ensure_candidate_slots(run, sampled)
            seed_ids_requiring_replacement = [
                seed_id
                for seed_id in mismatch_seed_ids
                if int((slots_by_seed.get(seed_id) or {}).get("candidate_revision") or 1)
                >= 2
            ]
            replacement_by_seed: dict[str, SeedProduct] = {}
            replacement_random_seed: int | None = None
            if seed_ids_requiring_replacement:
                eligible = self._eligible_replacement_seeds(run_id, sampled)
                if len(eligible) < len(seed_ids_requiring_replacement):
                    event = self.repo.append_run_event(
                        run_id,
                        "attribute_template.category_subject_replacement_unavailable",
                        "Repeated category conflicts exhausted their seed slots, but no safe replacement seed remains.",
                        {
                            "mismatch_seed_ids": mismatch_seed_ids,
                            "replacement_required_seed_ids": seed_ids_requiring_replacement,
                            "eligible_replacement_count": len(eligible),
                        },
                    )
                    return Result.failure(
                        "workbench.category_subject_replacement_unavailable",
                        "Repeated category conflicts require fresh seeds, but the eligible seed pool is exhausted.",
                        errors=subject_errors,
                        data=self._response_payload(run, event),
                    )
                replacement_random_seed = random.SystemRandom().randint(
                    1,
                    2**31 - 1,
                )
                replacements = self.repo.sample_seeds(
                    eligible,
                    len(seed_ids_requiring_replacement),
                    replacement_random_seed,
                )
                replacement_by_seed = dict(
                    zip(seed_ids_requiring_replacement, replacements)
                )

            retired: list[dict[str, Any]] = []
            for seed_id in mismatch_seed_ids:
                candidate = candidates[seed_id]
                product_id = str(candidate.get("ozon_product_id") or "").strip()
                reason = reason_by_seed[seed_id]
                replacement = replacement_by_seed.get(seed_id)
                replacement_seed_id = replacement.seed_id if replacement else seed_id
                self.repo.append_ozon_product_blacklist(
                    run_id,
                    product_id,
                    reason,
                    source_seed_id=seed_id,
                    reason_code="category_subject_mismatch",
                )
                slot = self._replace_candidate_slot(
                    run,
                    rejected_seed_id=seed_id,
                    replacement_seed_id=replacement_seed_id,
                    rejected_ozon_product_id=product_id,
                    reason=reason,
                )
                if replacement is not None:
                    rejected_seed = sampled_by_seed[seed_id]
                    self.repo.append_rejected_seed_attempt(
                        run_id,
                        rejected_seed,
                        reason,
                        replacement.seed_id,
                    )
                retired.append(
                    {
                        "seed_id": seed_id,
                        "replacement_seed_id": replacement_seed_id,
                        "ozon_product_id": product_id,
                        "slot_id": slot["slot_id"],
                        "candidate_revision": slot["candidate_revision"],
                        "reason": reason,
                    }
                )

            if replacement_by_seed:
                sampled = [
                    replacement_by_seed.get(seed.seed_id, seed)
                    for seed in sampled
                ]
                replacements = list(replacement_by_seed.values())
                self.repo.save_sampled_seeds(run_id, sampled)
                self.repo.append_used_seeds(
                    run_id,
                    replacements,
                    "workbench_category_subject_replacement_sampled",
                )
                run["sampled_seed_ids"] = [seed.seed_id for seed in sampled]
                run["rejected_seed_ids"] = [
                    item.get("seed_id")
                    for item in self.repo.load_rejected_seed_attempts(run_id)
                ]
                self._update_query_summary(run, sampled)

            mismatch_ids = set(mismatch_seed_ids)
            ozon_payload["ozon_candidates"] = [
                item
                for item in ozon_payload.get("ozon_candidates", [])
                if isinstance(item, dict)
                and str(item.get("seed_id") or "") not in mismatch_ids
            ]
            ozon_payload["updated_at"] = utc_now_iso()
            self.repo.save_ozon_collection_result(run_id, ozon_payload)

            try:
                existing_template_payload = self.repo.load_attribute_template_result(
                    run_id
                )
            except (FileNotFoundError, json.JSONDecodeError):
                existing_template_payload = {}
            existing_templates = {
                str(item.get("seed_id") or ""): item
                for item in existing_template_payload.get("seed_templates", [])
                if isinstance(item, dict) and item.get("seed_id")
            }
            submitted_templates = {
                str(item.get("seed_id") or ""): item
                for item in payload.get("seed_templates", [])
                if isinstance(item, dict) and item.get("seed_id")
            }
            retained_template_payload = {
                **existing_template_payload,
                **payload,
                "seed_templates": [
                    submitted_templates.get(seed_id)
                    or existing_templates[seed_id]
                    for seed_id in all_seed_ids
                    if seed_id not in mismatch_ids
                    and (
                        seed_id in submitted_templates
                        or seed_id in existing_templates
                    )
                ],
                "updated_at": utc_now_iso(),
            }
            self.repo.save_attribute_template_result(
                run_id,
                retained_template_payload,
            )

            for seed_id in mismatch_seed_ids:
                self._prune_excluded_product_artifacts(run_id, seed_id)
            for filename in (
                "attribute_template_contract.json",
                "ozon_collection_contract.json",
                "ozon_collection_draft.json",
                "supplier_review.json",
            ):
                (self.repo.run_dir(run_id) / filename).unlink(missing_ok=True)

            pending_ids = {
                replacement_by_seed.get(seed_id, sampled_by_seed[seed_id]).seed_id
                for seed_id in mismatch_seed_ids
            }
            run["replacement_pending_seed_ids"] = sorted(pending_ids)
            run["status"] = WorkbenchState.SEED_SELECTED.value
            run["ozon_collected"] = False
            run["attribute_template_collected"] = False
            run["ozon_collection_contract_ready"] = False
            run["attribute_template_contract_ready"] = False
            run["browser_task_cancelled"] = False
            run["browser_task_resumed_at"] = utc_now_iso()
            for key in (
                "attribute_template_contract_path",
                "ozon_collection_contract_path",
                "supplier_review_path",
            ):
                run.pop(key, None)
            self.repo.save_run(run)
            event = self.repo.append_run_event(
                run_id,
                "attribute_template.category_subject_candidates_retired",
                "Contradictory Ozon products were blacklisted and queued for exact-slot recollection.",
                {
                    "retired_candidates": retired,
                    "replacement_pending_seed_ids": sorted(pending_ids),
                    "replacement_random_seed": replacement_random_seed,
                    "retained_candidate_count": len(
                        ozon_payload.get("ozon_candidates", [])
                    ),
                },
            )
            return Result.success(
                "workbench.category_subject_candidates_retired",
                "Contradictory Ozon products were retired and replacement collection was queued.",
                self._response_payload(
                    run,
                    event,
                    {
                        "retired_candidates": retired,
                        "replacement_pending_seed_ids": sorted(pending_ids),
                    },
                ),
            )

    def _attach_seller_attribute_templates(self, payload: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(payload)
        enriched_templates: list[dict[str, Any]] = []
        for item in payload.get("seed_templates", []):
            seed_template = dict(item)
            public_evidence = dict(seed_template.get("public_attribute_evidence") or {})
            public_guess = seed_template.get("upload_attribute_schema")
            if public_guess:
                public_evidence["visible_public_schema_guess"] = public_guess
            category_candidate = dict(seed_template["category_candidates"][0])
            evidence = seed_template.get("evidence") or {}
            product_title = evidence.get("product_title") if isinstance(evidence, dict) else None
            product_url = evidence.get("product_url") if isinstance(evidence, dict) else None
            if product_title:
                category_candidate["product_title"] = product_title
            if product_url:
                category_candidate["product_url"] = product_url
            if seed_template.get("source_query"):
                category_candidate["source_query"] = seed_template["source_query"]
            public_attributes = public_evidence.get("attribute_table")
            if isinstance(public_attributes, dict):
                product_type = next(
                    (
                        value
                        for key, value in public_attributes.items()
                        if str(key).strip().casefold() in {"тип", "type"}
                        and str(value or "").strip()
                    ),
                    None,
                )
                if product_type:
                    category_candidate["product_type"] = product_type
            seller_template = self.seller_api_adapter.resolve_attribute_template(category_candidate)
            upload_schema = seller_template.get("upload_attribute_schema") or []
            seed_template["public_attribute_evidence"] = public_evidence
            seed_template["seller_attribute_template"] = {
                key: value for key, value in seller_template.items() if key != "upload_attribute_schema"
            }
            seed_template["upload_attribute_schema"] = upload_schema
            seed_template["draft_prefill_plan"] = self._build_draft_prefill_plan(upload_schema)
            enriched_templates.append(seed_template)
        enriched["seed_templates"] = enriched_templates
        enriched["schema_resolution"] = {
            "source": "ozon_seller_api_description_category_attribute",
            "public_page_schema_is_evidence_only": True,
        }
        return enriched

    def _build_draft_prefill_plan(self, upload_schema: list[dict[str, Any]]) -> list[dict[str, Any]]:
        plan: list[dict[str, Any]] = []
        for attribute in upload_schema:
            field_key = str(attribute.get("attribute_id") or "").strip()
            label = str(attribute.get("attribute_label") or "").casefold()
            field_text = f"{field_key} {label}".casefold()
            creative = any(token in field_text for token in CREATIVE_FIELDS_REQUIRING_REWRITE)
            objective_hint = any(token in field_text for token in OBJECTIVE_ATTRIBUTE_PREFILL_FIELDS)
            plan.append(
                {
                    "field_key": field_key,
                    "source": "seller_api_template_plus_supplier_or_ozon_objective_fact",
                    "prefill_allowed": not creative,
                    "rewrite_required": creative,
                    "reason": (
                        "Seller API upload field. Fill only when supplier/Ozon objective fact is available."
                        if objective_hint or not creative
                        else "Creative field must be rewritten, not copied from Ozon."
                    ),
                }
            )
        return plan

    def refresh_attribute_template(self, run_id: str, seed_id: str) -> Result:
        try:
            payload = self.repo.load_attribute_template_result(run_id)
            ozon_result = self.repo.load_ozon_collection_result(run_id)
        except FileNotFoundError:
            return Result.failure(
                "attribute_template.refresh_source_missing",
                "Stored category evidence and Ozon product evidence are required.",
                data={"run_id": run_id, "seed_id": seed_id},
            )
        seed_templates = payload.get("seed_templates", [])
        template_index = next(
            (
                index
                for index, item in enumerate(seed_templates)
                if isinstance(item, dict) and str(item.get("seed_id") or "") == seed_id
            ),
            None,
        )
        candidate = next(
            (
                item
                for item in ozon_result.get("ozon_candidates", [])
                if isinstance(item, dict) and str(item.get("seed_id") or "") == seed_id
            ),
            None,
        )
        if template_index is None or candidate is None:
            return Result.failure(
                "attribute_template.refresh_product_missing",
                "The requested batch product does not have both template and Ozon evidence.",
                data={"run_id": run_id, "seed_id": seed_id},
            )
        seed_template = dict(seed_templates[template_index])
        category_candidates = seed_template.get("category_candidates") or []
        if not category_candidates:
            return Result.failure(
                "attribute_template.refresh_category_missing",
                "The requested product has no frozen public category evidence.",
                data={"run_id": run_id, "seed_id": seed_id},
            )
        category_candidate = dict(category_candidates[0])
        category_candidate["product_title"] = candidate.get("title")
        product_type = next(
            (
                value
                for key, value in (candidate.get("attributes") or {}).items()
                if str(key).strip().casefold() in {"тип", "type"}
                and str(value or "").strip()
            ),
            "",
        )
        if product_type:
            category_candidate["product_type"] = product_type
        try:
            resolved = self.seller_api_adapter.resolve_attribute_template(category_candidate)
        except SellerApiError as exc:
            return Result.failure(
                "attribute_template.refresh_failed",
                "Seller API could not resolve a credible replacement category template.",
                errors=[str(exc)],
                data={"run_id": run_id, "seed_id": seed_id},
            )
        schema = resolved.get("upload_attribute_schema") or []
        assessment = assess_category_template_match(
            category_path=str(category_candidate.get("category_path") or ""),
            leaf_category=str(category_candidate.get("leaf_category") or ""),
            matched_category_path=str(resolved.get("matched_category_path") or ""),
            product_title=str(candidate.get("title") or ""),
            product_type=str(product_type or ""),
            category_url=str(category_candidate.get("category_url") or ""),
        )
        if not schema or not assessment["credible"]:
            return Result.failure(
                "attribute_template.refresh_mismatch",
                "The replacement template still conflicts with the product category evidence.",
                data={
                    "run_id": run_id,
                    "seed_id": seed_id,
                    "category_template_assessment": assessment,
                },
            )
        seed_template["seller_attribute_template"] = {
            key: value for key, value in resolved.items() if key != "upload_attribute_schema"
        }
        seed_template["upload_attribute_schema"] = schema
        seed_template["draft_prefill_plan"] = self._build_draft_prefill_plan(schema)
        seed_templates[template_index] = seed_template
        payload["seed_templates"] = seed_templates
        self.repo.save_attribute_template_result(run_id, payload)
        required_evidence_path = (
            self.repo.run_dir(run_id) / "required_attribute_evidence.json"
        )
        if required_evidence_path.exists():
            required_evidence = self.repo.load_required_attribute_evidence(
                run_id
            )
            evidence_items = required_evidence.get("items")
            if isinstance(evidence_items, dict) and seed_id in evidence_items:
                evidence_items.pop(seed_id, None)
                self.repo.save_required_attribute_evidence(
                    run_id,
                    required_evidence,
                )
        event = self.repo.append_run_event(
            run_id,
            "attribute_template.refreshed",
            "One mismatched Seller API category template was resolved again from frozen evidence.",
            {
                "seed_id": seed_id,
                "matched_category_path": resolved.get("matched_category_path"),
                "attribute_count": len(schema),
            },
        )
        return Result.success(
            "attribute_template.refreshed",
            "The product category template was refreshed. No product was published.",
            {
                "run_id": run_id,
                "seed_id": seed_id,
                "seller_attribute_template": seed_template["seller_attribute_template"],
                "attribute_count": len(schema),
                "last_event": event.to_dict(),
            },
        )

    def _seller_attribute_template_errors(self, run_id: str, expected_seed_ids: list[str]) -> list[str]:
        try:
            payload = self.repo.load_attribute_template_result(run_id)
        except FileNotFoundError:
            return ["Seller category attribute template result is missing."]
        errors = validate_attribute_template_result(
            payload,
            expected_seed_ids,
            require_seller_schema=True,
        )
        errors.extend(
            self._attribute_template_product_binding_errors(
                run_id,
                payload,
                expected_seed_ids,
            )
        )
        return errors

    def _attribute_template_product_binding_errors(
        self,
        run_id: str,
        payload: dict[str, Any],
        expected_seed_ids: list[str],
    ) -> list[str]:
        try:
            ozon_payload = self.repo.load_ozon_collection_result(run_id)
        except (FileNotFoundError, json.JSONDecodeError):
            return ["Final locked Ozon collection result is missing."]
        candidates = {
            str(item.get("seed_id") or ""): item
            for item in ozon_payload.get("ozon_candidates", [])
            if isinstance(item, dict) and item.get("seed_id")
        }
        templates = {
            str(item.get("seed_id") or ""): item
            for item in payload.get("seed_templates", [])
            if isinstance(item, dict) and item.get("seed_id")
        }
        errors: list[str] = []
        for seed_id in expected_seed_ids:
            candidate = candidates.get(seed_id)
            template = templates.get(seed_id)
            if candidate is None or template is None:
                continue
            expected_product_id = str(candidate.get("ozon_product_id") or "").strip()
            expected_slot_id = str(candidate.get("slot_id") or "").strip()
            expected_revision = int(candidate.get("candidate_revision") or 0)
            actual_product_id = str(template.get("source_ozon_product_id") or "").strip()
            actual_slot_id = str(template.get("slot_id") or "").strip()
            try:
                actual_revision = int(template.get("candidate_revision") or 0)
            except (TypeError, ValueError):
                actual_revision = 0
            if actual_product_id != expected_product_id:
                errors.append(
                    f"seed {seed_id} source_ozon_product_id {actual_product_id!r} does not match {expected_product_id!r}"
                )
            if actual_slot_id != expected_slot_id:
                errors.append(
                    f"seed {seed_id} slot_id {actual_slot_id!r} does not match {expected_slot_id!r}"
                )
            if actual_revision != expected_revision:
                errors.append(
                    f"seed {seed_id} candidate_revision {actual_revision!r} does not match {expected_revision!r}"
                )
            category_candidates = template.get("category_candidates") or []
            category_candidate = (
                category_candidates[0]
                if category_candidates and isinstance(category_candidates[0], dict)
                else {}
            )
            expected_category_id = str(candidate.get("category_id") or "").strip()
            actual_category_id = str(category_candidate.get("category_id") or "").strip()
            if expected_category_id and actual_category_id and actual_category_id != expected_category_id:
                errors.append(
                    f"seed {seed_id} category_id {actual_category_id!r} does not match final Ozon category {expected_category_id!r}"
                )
            evidence = template.get("evidence") or {}
            snapshot = evidence.get("public_product_snapshot") if isinstance(evidence, dict) else None
            snapshot_product_id = (
                str(snapshot.get("product_id") or "").strip()
                if isinstance(snapshot, dict)
                else ""
            )
            if snapshot_product_id and snapshot_product_id != expected_product_id:
                errors.append(
                    f"seed {seed_id} public snapshot product_id {snapshot_product_id!r} does not match {expected_product_id!r}"
                )
        return errors

    def _attribute_template_subject_errors(
        self,
        run_id: str,
        payload: dict[str, Any],
        expected_seed_ids: list[str],
    ) -> list[str]:
        try:
            ozon_payload = self.repo.load_ozon_collection_result(run_id)
        except (FileNotFoundError, json.JSONDecodeError):
            return ["Final locked Ozon collection result is missing."]
        candidates = {
            str(item.get("seed_id") or ""): item
            for item in ozon_payload.get("ozon_candidates", [])
            if isinstance(item, dict) and item.get("seed_id")
        }
        templates = {
            str(item.get("seed_id") or ""): item
            for item in payload.get("seed_templates", [])
            if isinstance(item, dict) and item.get("seed_id")
        }
        errors: list[str] = []
        for seed_id in expected_seed_ids:
            candidate = candidates.get(seed_id) or {}
            template = templates.get(seed_id) or {}
            attributes = candidate.get("attributes") or {}
            product_type = next(
                (
                    str(value).strip()
                    for key, value in attributes.items()
                    if str(key).strip().casefold()
                    in {"type", "тип", "类型", "商品类型", "褌懈锌"}
                    and str(value or "").strip()
                ),
                "",
            )
            if not product_type:
                continue
            seller_template = template.get("seller_attribute_template") or {}
            assessment = assess_category_template_match(
                category_path=str(candidate.get("category_path") or ""),
                leaf_category=str(candidate.get("leaf_category") or ""),
                matched_category_path=str(
                    seller_template.get("matched_category_path") or ""
                ),
                product_title=str(candidate.get("title") or ""),
                product_type=product_type,
                category_url=str(candidate.get("category_url") or ""),
            )
            if (
                assessment.get("product_type_score", 0) > 0
                and assessment.get("title_score", 0) == 0
            ):
                errors.append(
                    f"seed {seed_id} title does not describe the explicit Ozon product type {product_type!r}"
                )
        return errors

    def _store_duplicate_matches(
        self,
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        existing_products = self.repo.load_existing_products()
        matches: list[dict[str, Any]] = []
        for item in candidates:
            if not isinstance(item, dict):
                continue
            try:
                candidate = OzonCandidate.from_dict(item)
            except (KeyError, TypeError, ValueError):
                continue
            decision = decide_ozon_candidate_dedupe(candidate, existing_products)
            if decision.kind.value != "duplicate":
                continue
            matches.append(
                {
                    "seed_id": candidate.seed_id,
                    "ozon_product_id": candidate.ozon_product_id,
                    "matched_store_product_id": decision.matched_product_id,
                    "reason": decision.reason,
                    "evidence": decision.evidence,
                }
            )
        return matches

    def _frozen_contract_excluded_ozon_ids(self, run_id: str) -> set[str]:
        try:
            contract = self.repo.load_ozon_collection_contract(run_id)
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
            return set()
        payload = contract.get("payload") if isinstance(contract, dict) else None
        if not isinstance(payload, dict):
            return set()
        return {
            str(product_id).strip().casefold()
            for product_id in payload.get("excluded_ozon_product_ids", [])
            if str(product_id).strip()
        }

    def _reject_used_ozon_identities(
        self,
        run: dict[str, Any],
        conflicts: list[dict[str, Any]],
    ) -> Result:
        event = self.repo.append_run_event(
            run["run_id"],
            "ozon_collection.product_identity_blocked",
            "Ozon collection was blocked by the persistent product identity ledger.",
            {"conflicts": conflicts},
        )
        return Result.failure(
            "workbench.ozon_product_identity_used",
            "This Ozon product was already excluded or used by another batch.",
            data=self._response_payload(run, event, {"conflicts": conflicts}),
        )

    def ozon_collection_checkpoint(self, run_id: str) -> Result:
        try:
            contract = self.repo.load_ozon_collection_contract(run_id)
            contract_payload = contract.get("payload")
            if not isinstance(contract_payload, dict):
                raise TypeError("Ozon collection contract payload must be an object")
            raw_seeds = contract_payload.get("seeds")
            if not isinstance(raw_seeds, list) or not raw_seeds:
                raise ValueError("Ozon collection contract must include seeds")
            contract_seed_ids = [
                str(seed.get("seed_id") or "").strip()
                for seed in raw_seeds
                if isinstance(seed, dict)
            ]
            if (
                len(contract_seed_ids) != len(raw_seeds)
                or any(not seed_id for seed_id in contract_seed_ids)
                or len(set(contract_seed_ids)) != len(contract_seed_ids)
            ):
                raise ValueError("Ozon collection contract has invalid seed ids")
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
            return Result.failure(
                "ozon_collection.checkpoint_invalid",
                "The frozen Ozon collection contract is missing or invalid.",
                errors=[str(exc)],
                data={"run_id": run_id},
            )

        excluded_product_ids = {
            str(product_id).strip()
            for product_id in contract_payload.get("excluded_ozon_product_ids", [])
            if str(product_id).strip()
        }
        excluded_product_ids.update(self.repo.load_blacklisted_ozon_product_ids())
        draft_path = self.repo.run_dir(run_id) / "ozon_collection_draft.json"
        if not draft_path.exists():
            return Result.success(
                "ozon_collection.checkpoint_empty",
                "No Ozon collection checkpoint has been saved yet.",
                {
                    "run_id": run_id,
                    "contract_seed_ids": contract_seed_ids,
                    "excluded_ozon_product_ids": sorted(excluded_product_ids),
                    "ozon_candidates": [],
                    "created_at": None,
                    "updated_at": None,
                },
            )

        try:
            draft = self.repo.load_ozon_collection_draft(run_id)
            if not isinstance(draft, dict):
                raise TypeError("Ozon collection checkpoint must be an object")
            if str(draft.get("run_id") or "") != run_id:
                raise ValueError("Ozon collection checkpoint run_id does not match the batch")
            candidates = draft.get("ozon_candidates")
            if not isinstance(candidates, list):
                raise TypeError("Ozon collection checkpoint candidates must be a list")
            worker = str(draft.get("worker") or "")
            errors: list[str] = []
            candidate_seed_ids: list[str] = []
            candidate_product_ids: list[str] = []
            for index, candidate in enumerate(candidates, start=1):
                if not isinstance(candidate, dict):
                    errors.append(f"Ozon checkpoint candidate {index} must be an object")
                    continue
                seed_id = str(candidate.get("seed_id") or "").strip()
                product_id = str(candidate.get("ozon_product_id") or "").strip()
                candidate_seed_ids.append(seed_id)
                candidate_product_ids.append(product_id)
                if seed_id not in contract_seed_ids:
                    errors.append(f"Ozon checkpoint candidate has unexpected seed_id: {seed_id}")
                    continue
                errors.extend(
                    validate_ozon_collection_result(
                        {"worker": worker, "ozon_candidates": [candidate]},
                        [seed_id],
                    )
                )
                if product_id in excluded_product_ids:
                    errors.append(f"Ozon checkpoint candidate uses excluded product id: {product_id}")
            if len(candidate_seed_ids) != len(set(candidate_seed_ids)):
                errors.append("Ozon collection checkpoint has duplicate seed ids")
            if len(candidate_product_ids) != len(set(candidate_product_ids)):
                errors.append("Ozon collection checkpoint has duplicate product ids")
            captured_seed_ids = set(candidate_seed_ids)
            expected_order = [seed_id for seed_id in contract_seed_ids if seed_id in captured_seed_ids]
            if candidate_seed_ids != expected_order:
                errors.append("Ozon collection checkpoint candidates are not in frozen contract order")
            if errors:
                raise ValueError("; ".join(errors))
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
            return Result.failure(
                "ozon_collection.checkpoint_invalid",
                "The saved Ozon collection checkpoint is invalid and was not overwritten.",
                errors=[str(exc)],
                data={"run_id": run_id, "checkpoint_path": str(draft_path)},
            )

        return Result.success(
            "ozon_collection.checkpoint_loaded",
            "The verified Ozon collection checkpoint was loaded.",
            {
                "run_id": run_id,
                "contract_seed_ids": contract_seed_ids,
                "excluded_ozon_product_ids": sorted(excluded_product_ids),
                "ozon_candidates": candidates,
                "created_at": draft.get("created_at"),
                "updated_at": draft.get("updated_at"),
                "checkpoint_path": str(draft_path),
            },
        )

    def save_ozon_collection_progress(self, run_id: str, payload: dict[str, Any]) -> Result:
        run = self.repo.load_run(run_id)
        if WorkbenchState(run["status"]) != WorkbenchState.OZON_COLLECTING:
            return Result.failure(
                "ozon_collection.progress_not_expected",
                "Ozon collection progress is only accepted while Ozon collection is active.",
                data={"run_id": run_id, "status": run["status"]},
            )
        if str(payload.get("run_id") or "") != run_id:
            return Result.failure(
                "ozon_collection.progress_run_mismatch",
                "Ozon collection progress belongs to a different batch.",
                data={"run_id": run_id, "payload_run_id": payload.get("run_id")},
            )
        candidate = payload.get("ozon_candidate")
        if not isinstance(candidate, dict):
            return Result.failure(
                "ozon_collection.progress_invalid",
                "Ozon collection progress must include one candidate object.",
                data={"run_id": run_id},
            )

        checkpoint = self.ozon_collection_checkpoint(run_id)
        if not checkpoint.ok:
            return checkpoint
        contract_seed_ids = list(checkpoint.data["contract_seed_ids"])
        excluded_product_ids = set(checkpoint.data["excluded_ozon_product_ids"])
        seed_id = str(candidate.get("seed_id") or "").strip()
        if seed_id not in contract_seed_ids:
            return Result.failure(
                "ozon_collection.progress_seed_not_expected",
                "The Ozon candidate seed does not belong to the frozen collection contract.",
                data={"run_id": run_id, "seed_id": seed_id},
            )
        errors = validate_ozon_collection_result(
            {
                "worker": payload.get("worker"),
                "source": payload.get("source"),
                "ozon_candidates": [candidate],
            },
            [seed_id],
        )
        product_id = str(candidate.get("ozon_product_id") or "").strip()
        if product_id in excluded_product_ids:
            errors.append(f"Ozon product id is excluded from collection: {product_id}")
        if errors:
            return Result.failure(
                "ozon_collection.progress_invalid",
                "The Ozon collection checkpoint candidate failed validation.",
                errors=errors,
                data={"run_id": run_id, "seed_id": seed_id},
            )
        duplicate_matches = self._store_duplicate_matches([candidate])
        if duplicate_matches:
            event = self.repo.append_run_event(
                run_id,
                "ozon_collection.store_duplicate_blocked",
                "An Ozon checkpoint candidate was blocked because it already exists in the store history.",
                {"duplicates": duplicate_matches},
            )
            return Result.failure(
                "workbench.store_duplicate_detected",
                "This product already exists in the store history and cannot be checkpointed for upload.",
                data=self._response_payload(
                    run,
                    event,
                    {"duplicates": duplicate_matches},
                ),
            )

        candidate_by_seed = {
            str(item.get("seed_id") or ""): item
            for item in checkpoint.data["ozon_candidates"]
        }
        existing = candidate_by_seed.get(seed_id)
        if existing is not None and existing != candidate:
            return Result.failure(
                "ozon_collection.progress_conflict",
                "This Ozon seed already has a different verified checkpoint candidate.",
                data={"run_id": run_id, "seed_id": seed_id},
            )
        for other_seed_id, other in candidate_by_seed.items():
            if (
                other_seed_id != seed_id
                and str(other.get("ozon_product_id") or "").strip() == product_id
            ):
                return Result.failure(
                    "ozon_collection.progress_duplicate_product",
                    "The Ozon product is already checkpointed for another seed.",
                    data={
                        "run_id": run_id,
                        "seed_id": seed_id,
                        "ozon_product_id": product_id,
                    },
                )

        candidate_by_seed[seed_id] = candidate
        ordered_candidates = [
            candidate_by_seed[item_id]
            for item_id in contract_seed_ids
            if item_id in candidate_by_seed
        ]
        now = utc_now_iso()
        draft = {
            "schema_version": 1,
            "run_id": run_id,
            "worker": str(payload.get("worker") or ""),
            "source": str(payload.get("source") or ""),
            "ozon_candidates": ordered_candidates,
            "created_at": checkpoint.data.get("created_at") or now,
            "updated_at": now,
        }
        draft_path = self.repo.save_ozon_collection_draft(run_id, draft)
        return Result.success(
            "ozon_collection.progress_saved",
            "The verified Ozon candidate checkpoint was saved.",
            {
                "run_id": run_id,
                "status": run["status"],
                "seed_id": seed_id,
                "completed_count": len(ordered_candidates),
                "total_count": len(contract_seed_ids),
                "checkpoint_path": str(draft_path),
                "ozon_candidates": ordered_candidates,
            },
        )

    def restart_browser_task(self, run_id: str) -> Result:
        run = self.recover_browser_task_state(run_id)
        run = self._recover_pending_supplier_replacement(run)
        status = WorkbenchState(run["status"])
        recapture_seed_ids = {
            str(seed_id).strip()
            for seed_id in run.get("supplier_recapture_seed_ids") or []
            if str(seed_id).strip()
        }
        task_type_by_status = {
            WorkbenchState.OZON_COLLECTING: "ozon_collection",
            WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTING: "ozon_attribute_template",
            WorkbenchState.SUPPLIER_REVIEW: "supplier_selection",
            WorkbenchState.SUPPLIER_COLLECTING: "supplier_collection",
        }
        if status == WorkbenchState.OZON_COLLECTED:
            prepared = self.dispatch(
                run_id,
                WorkbenchAction.START_ATTRIBUTE_TEMPLATE_COLLECTION.value,
            )
            if not prepared.ok:
                return prepared
            run = self.repo.load_run(run_id)
            status = WorkbenchState(run["status"])
        task_type = (
            "supplier_selection"
            if recapture_seed_ids
            and status in {WorkbenchState.SUPPLIER_COLLECTED, WorkbenchState.IMAGE_PROCESSING}
            else task_type_by_status.get(status)
        )
        if task_type is None:
            return Result.failure(
                "browser_task.restart_not_allowed",
                "The current batch stage has no restartable browser collection task.",
                data={"run_id": run_id, "status": run["status"]},
            )

        completed_count = 0
        total_count = 0
        try:
            if task_type == "ozon_collection":
                self._reconcile_replacement_ozon_checkpoint(run)
                result_path = self.repo.run_dir(run_id) / "ozon_collection_result.json"
                if result_path.exists() and not run.get("replacement_pending_seed_ids"):
                    return Result.failure(
                        "browser_task.restart_completed",
                        "The formal Ozon collection result already exists.",
                        data={"run_id": run_id, "status": run["status"], "task_type": task_type},
                    )
                checkpoint = self.ozon_collection_checkpoint(run_id)
                if not checkpoint.ok:
                    return Result.failure(
                        "browser_task.restart_checkpoint_invalid",
                        "The saved Ozon collection checkpoint is invalid and was not overwritten.",
                        errors=checkpoint.errors,
                        data=checkpoint.data,
                    )
                total_count = len(checkpoint.data["contract_seed_ids"])
                completed_count = len(checkpoint.data["ozon_candidates"])
            elif task_type == "ozon_attribute_template":
                contract = self.repo.load_attribute_template_contract(run_id)
                payload = contract.get("payload") if isinstance(contract, dict) else None
                seeds = payload.get("seeds") if isinstance(payload, dict) else None
                if not isinstance(seeds, list) or not seeds:
                    raise ValueError("Attribute template contract must include locked Ozon products")
                expected_seed_ids = {
                    str(item.get("seed_id") or "").strip()
                    for item in seeds
                    if isinstance(item, dict) and str(item.get("seed_id") or "").strip()
                }
                total_count = len(expected_seed_ids)
                completed_seed_ids: set[str] = set()
                try:
                    template_result = self.repo.load_attribute_template_result(run_id)
                except (FileNotFoundError, json.JSONDecodeError, OSError):
                    template_result = {}
                completed_seed_ids = {
                    str(item.get("seed_id") or "").strip()
                    for item in template_result.get("seed_templates", [])
                    if isinstance(item, dict) and str(item.get("seed_id") or "").strip()
                }
                completed_count = len(expected_seed_ids.intersection(completed_seed_ids))
                if completed_count >= total_count:
                    return Result.failure(
                        "browser_task.restart_completed",
                        "The exact-product attribute template result already exists.",
                        data={"run_id": run_id, "status": run["status"], "task_type": task_type},
                    )
            elif task_type == "supplier_selection":
                review = self.repo.load_supplier_review(run_id)
                items = review.get("items")
                if not isinstance(items, list) or not items:
                    raise ValueError("Supplier review must include collection items")
                review_seed_ids = {
                    str(item.get("seed_id") or "").strip()
                    for item in items
                    if isinstance(item, dict) and str(item.get("seed_id") or "").strip()
                }
                expected_seed_ids = recapture_seed_ids or review_seed_ids
                total_count = len(expected_seed_ids)
                draft_path = self.repo.run_dir(run_id) / "supplier_selection_draft.json"
                captured_seed_ids: set[str] = set()
                if draft_path.exists():
                    draft = self.repo.load_supplier_selection_draft(run_id)
                    if str(draft.get("run_id") or "") != run_id:
                        raise ValueError("Supplier selection checkpoint belongs to another batch")
                    products = draft.get("supplier_products")
                    if not isinstance(products, list):
                        raise TypeError("Supplier selection checkpoint products must be a list")
                    captured_seed_ids = {
                        str(product.get("seed_id") or "").strip()
                        for product in products
                        if isinstance(product, dict) and str(product.get("seed_id") or "").strip()
                    }
                completed_count = len(expected_seed_ids.intersection(captured_seed_ids))
                if completed_count >= total_count:
                    return Result.failure(
                        "browser_task.restart_completed",
                        "All managed 1688 supplier-selection channels are already complete.",
                        data={"run_id": run_id, "status": run["status"], "task_type": task_type},
                    )
            else:
                result_path = self.repo.run_dir(run_id) / "supplier_collection_result.json"
                if result_path.exists():
                    return Result.failure(
                        "browser_task.restart_completed",
                        "The formal 1688 supplier collection result already exists.",
                        data={"run_id": run_id, "status": run["status"], "task_type": task_type},
                    )
                contract = self.repo.load_supplier_collection_contract(run_id)
                items = contract.get("items")
                if not isinstance(items, list) or not items:
                    raise ValueError("Supplier collection contract must include items")
                total_count = len(items)
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
            return Result.failure(
                "browser_task.restart_checkpoint_invalid",
                "The saved browser collection contract or checkpoint is invalid and was not overwritten.",
                errors=[str(exc)],
                data={"run_id": run_id, "status": run["status"], "task_type": task_type},
            )

        pending_count = max(total_count - completed_count, 0)
        dispatch_token = utc_now_iso()
        run["browser_task_cancelled"] = False
        run.pop("browser_task_cancelled_at", None)
        run.pop("browser_task_cancel_reason", None)
        run["browser_task_resumed_at"] = dispatch_token
        self.repo.save_run(run)
        event = self.repo.append_run_event(
            run_id,
            "browser_task.user_restart_requested",
            "The user requested a safe restart of the current browser collection task.",
            {
                "task_type": task_type,
                "completed_count": completed_count,
                "pending_count": pending_count,
                "dispatch_token": dispatch_token,
            },
        )
        return Result.success(
            "browser_task.restart_requested",
            "The current browser collection task was queued for redispatch.",
            {
                "run_id": run_id,
                "status": run["status"],
                "task_type": task_type,
                "completed_count": completed_count,
                "pending_count": pending_count,
                "dispatch_token": dispatch_token,
                "last_event": event.to_dict(),
            },
        )

    def ingest_ozon_collection_result(self, run_id: str, payload: dict[str, Any]) -> Result:
        run = self.repo.load_run(run_id)
        if WorkbenchState(run["status"]) != WorkbenchState.OZON_COLLECTING:
            event = self.repo.append_run_event(
                run_id,
                "ozon_collection.ingest_rejected",
                "Ozon collection result was rejected because the batch is not waiting for Ozon collection.",
                {"status": run["status"]},
            )
            return Result.failure(
                "workbench.ozon_collection_not_expected",
                "Ozon collection result is only accepted while status is ozon_collecting.",
                data=self._response_payload(run, event),
            )
        dispatch_token = self._browser_dispatch_token(run)
        payload_dispatch_token = str(payload.get("dispatch_token") or "").strip()
        if payload_dispatch_token and payload_dispatch_token != dispatch_token:
            return self._reject_stale_stage_result(
                run_id,
                "ozon_collection",
                run,
                ["browser result dispatch token is stale"],
            )
        seeds = self._safe_load_sampled_seeds(run_id)
        all_seed_ids = [seed.seed_id for seed in seeds]
        expected_seed_ids = self._pending_or_all_seed_ids(
            run,
            all_seed_ids,
            result_kind="ozon_collection",
        )
        candidate_items = [
            item
            for item in payload.get("ozon_candidates", [])
            if isinstance(item, dict)
        ]
        full_current_snapshot = self._is_full_current_seed_snapshot(
            candidate_items,
            all_seed_ids,
        )
        if full_current_snapshot:
            expected_seed_ids = all_seed_ids
        frozen_excluded_ids = self._frozen_contract_excluded_ozon_ids(run_id)
        frozen_conflicts = [
            {
                "identity_type": "ozon_product_id",
                "identity_value": str(item.get("ozon_product_id") or "").strip(),
                "requested_run_id": run_id,
                "requested_seed_id": str(item.get("seed_id") or ""),
                "previous_source": "frozen_ozon_collection_contract",
            }
            for item in candidate_items
            if str(item.get("ozon_product_id") or "").strip().casefold()
            in frozen_excluded_ids
        ]
        if frozen_conflicts:
            return self._reject_used_ozon_identities(run, frozen_conflicts)
        duplicate_matches = self._store_duplicate_matches(
            candidate_items
        )
        if duplicate_matches:
            event = self.repo.append_run_event(
                run_id,
                "ozon_collection.store_duplicate_blocked",
                "Ozon collection was blocked because one or more products already exist in the store history.",
                {"duplicates": duplicate_matches},
            )
            return Result.failure(
                "workbench.store_duplicate_detected",
                "A collected Ozon product already exists in the store history and cannot continue to upload.",
                data=self._response_payload(
                    run,
                    event,
                    {"duplicates": duplicate_matches},
                ),
            )
        errors = validate_ozon_collection_result(payload, expected_seed_ids)
        blacklisted_product_ids = self.repo.load_blacklisted_ozon_product_ids()
        collected_blacklisted_ids = sorted(
            {
                str(item.get("ozon_product_id"))
                for item in payload.get("ozon_candidates", [])
                if isinstance(item, dict) and str(item.get("ozon_product_id") or "") in blacklisted_product_ids
            }
        )
        if collected_blacklisted_ids:
            errors.append(f"Blacklisted Ozon product ids were collected: {', '.join(collected_blacklisted_ids)}")
        if errors:
            event = self.repo.append_run_event(
                run_id,
                "ozon_collection.ingest_invalid",
                "Ozon collection result failed validation.",
                {"errors": errors},
            )
            return Result.failure(
                "workbench.ozon_collection_invalid",
                "Ozon collection result failed validation.",
                errors=errors,
                data=self._response_payload(run, event),
            )
        with self._run_mutation_lock(run_id):
            current_run = self.repo.load_run(run_id)
            current_seed_ids = [
                seed.seed_id for seed in self._safe_load_sampled_seeds(run_id)
            ]
            stale_errors = self._stage_snapshot_errors(
                run=current_run,
                expected_state=WorkbenchState.OZON_COLLECTING,
                expected_seed_ids=all_seed_ids,
                current_seed_ids=current_seed_ids,
                expected_dispatch_token=dispatch_token,
                payload_dispatch_token=payload_dispatch_token,
            )
            if stale_errors:
                return self._reject_stale_stage_result(
                    run_id,
                    "ozon_collection",
                    current_run,
                    stale_errors,
                )
            run = current_run
            if run.get("replacement_pending_seed_ids") and not full_current_snapshot:
                try:
                    existing_payload = self.repo.load_ozon_collection_result(run_id)
                except FileNotFoundError:
                    existing_payload = {}
                pending_ids = set(expected_seed_ids)
                retained_candidates = [
                    item
                    for item in existing_payload.get("ozon_candidates", [])
                    if isinstance(item, dict) and str(item.get("seed_id") or "") not in pending_ids
                ]
                payload = {**payload, "ozon_candidates": retained_candidates + list(payload.get("ozon_candidates", []))}
                merged_errors = validate_ozon_collection_result(payload, all_seed_ids)
                if merged_errors:
                    event = self.repo.append_run_event(
                        run_id,
                        "ozon_collection.merge_invalid",
                        "Replacement Ozon result could not be merged with retained evidence.",
                        {"errors": merged_errors},
                    )
                    return Result.failure(
                        "workbench.ozon_collection_merge_invalid",
                        "Replacement Ozon result could not be merged with retained evidence.",
                        errors=merged_errors,
                        data=self._response_payload(run, event),
                    )
            identity_errors = self._stamp_ozon_candidate_identities(
                run,
                [
                    item
                    for item in payload.get("ozon_candidates", [])
                    if isinstance(item, dict)
                ],
            )
            if identity_errors:
                event = self.repo.append_run_event(
                    run_id,
                    "ozon_collection.candidate_identity_invalid",
                    "Ozon collection result did not match the active candidate slot revision.",
                    {"errors": identity_errors},
                )
                return Result.failure(
                    "workbench.ozon_candidate_identity_invalid",
                    "Ozon collection result did not match the active candidate slot revision.",
                    errors=identity_errors,
                    data=self._response_payload(run, event),
                )
            identity_conflicts = self.repo.claim_product_identities(
                run_id,
                [
                    {
                        "identity_type": "ozon_product_id",
                        "identity_value": item.get("ozon_product_id"),
                        "seed_id": item.get("seed_id"),
                        "source": "ozon_collection_ingest",
                    }
                    for item in payload.get("ozon_candidates", [])
                    if isinstance(item, dict)
                ],
            )
            if identity_conflicts:
                return self._reject_used_ozon_identities(run, identity_conflicts)
            result_path = self.repo.save_ozon_collection_result(run_id, payload)
            current = WorkbenchState(run["status"])
            run["status"] = transition_workbench_state(current, WorkbenchAction.MARK_OZON_COLLECTED).value
            run["ozon_collected"] = True
            run["ozon_collection_result_path"] = str(result_path)
            run["attribute_template_collected"] = False
            run["attribute_template_contract_ready"] = False
            run.pop("attribute_template_contract_path", None)
            self.repo.save_run(run)
            event = self.repo.append_run_event(
                run_id,
                "ozon_collection.ingested",
                "Ozon collection result was ingested; final products are locked for exact category template collection.",
                {"result_path": str(result_path), "candidate_count": len(payload.get("ozon_candidates", []))},
            )
            return Result.success(
                "workbench.ozon_collection_ingested",
                "Ozon products are locked. Exact category template collection is required next.",
                self._response_payload(run, event),
            )

    def supplier_review(self, run_id: str) -> Result:
        run = self.recover_browser_task_state(run_id)
        try:
            review = self.repo.load_supplier_review(run_id)
        except FileNotFoundError:
            return Result.failure(
                "supplier_review.missing",
                "Supplier review data is not available for this batch.",
                data={"run_id": run_id, "status": run["status"]},
            )
        progress_path = self.repo.run_dir(run_id) / "supplier_collection_progress.json"
        collection_progress = self.repo.load_supplier_collection_progress(run_id) if progress_path.exists() else None
        items = self._collection_review_items(run_id, review, collection_progress)
        can_approve = WorkbenchState(run["status"]) == WorkbenchState.SUPPLIER_COLLECTED and bool(items) and all(
            not item["ozon_completeness"]["missing_fields"]
            and not item["supplier_completeness"]["missing_fields"]
            for item in items
        )
        return Result.success(
            "supplier_review.loaded",
            "Supplier review data loaded.",
            {
                "run_id": run_id,
                "status": run["status"],
                "items": items,
                "ready_to_collect": self._supplier_review_complete(review),
                "collection_progress": collection_progress,
                "can_approve": can_approve,
            },
        )

    def save_supplier_collection_progress(self, run_id: str, payload: dict[str, Any]) -> Result:
        run = self.repo.load_run(run_id)
        if WorkbenchState(run["status"]) != WorkbenchState.SUPPLIER_COLLECTING:
            return Result.failure(
                "supplier_collection.progress_not_expected",
                "Supplier collection progress is only accepted during supplier collection.",
                data={"run_id": run_id, "status": run["status"]},
            )
        contract = self.repo.load_supplier_collection_contract(run_id)
        expected = {
            str(item.get("seed_id") or ""): str(item.get("supplier_url") or "")
            for item in contract.get("items", [])
        }
        seed_id = str(payload.get("seed_id") or "")
        supplier_url = str(payload.get("supplier_url") or "")
        if seed_id not in expected or supplier_url != expected[seed_id]:
            return Result.failure(
                "supplier_collection.progress_invalid",
                "Supplier collection progress does not match the user-verified link.",
                data={"run_id": run_id, "seed_id": seed_id},
            )
        progress = {
            "run_id": run_id,
            "seed_id": seed_id,
            "supplier_url": supplier_url,
            "partial_product": payload.get("partial_product") if isinstance(payload.get("partial_product"), dict) else {},
            "missing_fields": payload.get("missing_fields") if isinstance(payload.get("missing_fields"), list) else [],
            "updated_at": utc_now_iso(),
        }
        path = self.repo.save_supplier_collection_progress(run_id, progress)
        return Result.success(
            "supplier_collection.progress_saved",
            "Partial 1688 supplier evidence was saved.",
            {"run_id": run_id, "status": run["status"], "progress_path": str(path), "collection_progress": progress},
        )

    def image_workspace(self, run_id: str) -> Result:
        run = self.repo.load_run(run_id)
        try:
            ozon_result = self.repo.load_ozon_collection_result(run_id)
        except FileNotFoundError:
            return Result.failure(
                "image_workspace.ozon_source_missing",
                "Ozon reference images are not available for this batch.",
                data={"run_id": run_id, "status": run["status"]},
            )

        supplier_products: list[dict[str, Any]] = []
        supplier_result_path = self.repo.run_dir(run_id) / "supplier_collection_result.json"
        if supplier_result_path.exists():
            supplier_result = self.repo.load_supplier_collection_result(run_id)
            if isinstance(supplier_result.get("supplier_products"), list):
                supplier_products = supplier_result["supplier_products"]
        suppliers_by_seed = {
            str(product.get("seed_id") or ""): product for product in supplier_products
        }
        selection_path = self.repo.run_dir(run_id) / "supplier_sku_selections.json"
        selections = (
            self.repo.load_supplier_sku_selections(run_id).get("selections", {})
            if selection_path.exists()
            else {}
        )
        subject_path = self.repo.run_dir(run_id) / "subject_masters.json"
        subject_items = (
            self.repo.load_subject_masters(run_id).get("items", {})
            if subject_path.exists()
            else {}
        )

        items: list[dict[str, Any]] = []
        candidates = ozon_result.get("ozon_candidates") if isinstance(ozon_result.get("ozon_candidates"), list) else []
        for candidate in candidates:
            seed_id = str(candidate.get("seed_id") or "")
            media = candidate.get("selected_sku_media") or {}
            ozon_images = [
                str(url)
                for url in (
                    media.get("selected_sku_images")
                    or media.get("main_gallery_images")
                    or []
                )
                if str(url).strip()
            ]
            ozon_reference_inputs = [
                {"reference_slot_index": index, "url": url}
                for index, url in enumerate(ozon_images, start=1)
            ]
            supplier = suppliers_by_seed.get(seed_id, {})
            supplier_images = self._supplier_product_images(supplier)
            selection = selections.get(seed_id) if isinstance(selections, dict) else None
            subject_entry = subject_items.get(seed_id) if isinstance(subject_items, dict) else None
            subject_master = (
                subject_entry.get("subject_master")
                if isinstance(subject_entry, dict) and isinstance(subject_entry.get("subject_master"), dict)
                else None
            )
            job_id = str(subject_entry.get("image_job_id") or "") if isinstance(subject_entry, dict) else ""
            image_job = None
            if job_id:
                try:
                    image_job = self._image_job_payload(job_id)
                except ValueError:
                    image_job = None
            if image_job:
                for slot in image_job.get("slots", []):
                    mapping = slot.get("ozon_reference_mapping")
                    if not isinstance(mapping, dict):
                        continue
                    reference_index = mapping.get("reference_slot_index")
                    if (
                        isinstance(reference_index, int)
                        and not isinstance(reference_index, bool)
                        and 1 <= reference_index <= len(ozon_images)
                    ):
                        mapping["reference_url"] = ozon_images[reference_index - 1]
            generated_images = [
                str(slot.get("accepted_path") or "")
                for slot in (image_job or {}).get("slots", [])
                if str(slot.get("accepted_path") or "")
            ]
            if image_job:
                generation_status = str(image_job.get("status") or "pending")
            elif selection and subject_master:
                generation_status = "queue_missing"
            elif selection:
                generation_status = "waiting_for_subject_master"
            else:
                generation_status = "waiting_for_supplier_sku"
            items.append(
                {
                    "seed_id": seed_id,
                    "ozon_product_id": candidate.get("ozon_product_id"),
                    "ozon_title": candidate.get("title"),
                    "ozon_url": candidate.get("ozon_url"),
                    "selected_options": (candidate.get("target_sku") or {}).get("selected_options") or {},
                    "ozon_reference_images": ozon_images,
                    "ozon_reference_inputs": ozon_reference_inputs,
                    "supplier_title": supplier.get("title"),
                    "supplier_url": supplier.get("supplier_url"),
                    "supplier_source_images": supplier_images,
                    "supplier_sku_options": supplier.get("sku_options") or [],
                    "supplier_sku_selection": selection,
                    "subject_master": subject_master,
                    "image_job": image_job,
                    "generated_images": generated_images,
                    "generation_status": generation_status,
                }
            )

        generated_count = sum(len(item["generated_images"]) for item in items)
        queue_state_keys = (
            "manual_review_required",
            "repair_pending",
            "pending",
            "in_progress",
            "stopped",
            "completed",
            "failed",
            "waiting_for_supplier_sku",
            "waiting_for_subject_master",
            "queue_missing",
        )
        image_queue_summary = {key: 0 for key in queue_state_keys}
        image_queue_summary["total_products"] = len(items)
        for item in items:
            image_job = item.get("image_job") or {}
            has_repair_pending = any(
                slot.get("status") == "repair_pending"
                for slot in image_job.get("slots", [])
            )
            summary_state = (
                "repair_pending"
                if image_job.get("status") == "pending" and has_repair_pending
                else item["generation_status"]
            )
            if summary_state in image_queue_summary:
                image_queue_summary[summary_state] += 1
        all_selected = bool(items) and all(item["supplier_sku_selection"] for item in items)
        all_subjects = bool(items) and all(item["subject_master"] for item in items)
        all_jobs = bool(items) and all(item["image_job"] for item in items)
        all_generated = all_jobs and all(
            item["image_job"].get("status") in {"manual_review_required", "completed"}
            for item in items
        )
        all_approved = all_jobs and all(
            item["image_job"].get("status") == "completed" for item in items
        )
        if not all_selected:
            gate_code = "supplier_sku_selection_required"
            gate_message = "请先为每个产品锁定一个真实 1688 SKU (Lock one real 1688 SKU for every product)."
        elif not all_subjects:
            gate_code = "subject_master_required"
            gate_message = "请从已锁定 SKU 与供应商商品图中确认主体证据 (Confirm subject evidence for every locked SKU)."
        elif not all_jobs:
            gate_code = "image_queue_missing"
            gate_message = "主体已确认，但本地生图任务尚未完整入列 (Image queue entry is missing)."
        elif not all_generated:
            gate_code = "waiting_for_codex_workers"
            gate_message = "任务已进入本地队列，等待 Ozon 专用生图技能按单线程顺序处理 (Waiting for the dedicated single-thread image skill)."
        elif not all_approved:
            gate_code = "image_review_required"
            gate_message = "八张图片已回写；请逐件审核，合格后点击“确认本件 8 张图片可用” (Review and approve each eight-image set)."
        else:
            gate_code = "images_approved"
            gate_message = "全部商品图片已由用户确认，可以通过上传图片门禁 (All image sets are approved)."

        return Result.success(
            "image_workspace.loaded",
            "Image workspace source assets loaded.",
            {
                "run_id": run_id,
                "status": run["status"],
                "items": items,
                "source_counts": {
                    "ozon_reference_images": sum(len(item["ozon_reference_images"]) for item in items),
                    "supplier_source_images": sum(len(item["supplier_source_images"]) for item in items),
                    "generated_images": generated_count,
                },
                "image_queue_summary": image_queue_summary,
                "image_gate": {
                    "ready": all_approved,
                    "code": gate_code,
                    "message": gate_message,
                },
            },
        )

    def preview_pricing_evidence(
        self, run_id: str, payload: dict[str, Any]
    ) -> Result:
        return self._calculate_pricing_evidence(
            run_id,
            payload,
            confirmed=False,
        )

    def confirm_pricing_evidence(
        self, run_id: str, payload: dict[str, Any]
    ) -> Result:
        calculated = self._calculate_pricing_evidence(
            run_id,
            payload,
            confirmed=True,
        )
        if not calculated.ok:
            return calculated

        seed_id = str(calculated.data["seed_id"])
        evidence_path = self.repo.run_dir(run_id) / "pricing_evidence.json"
        if evidence_path.exists():
            stored = self.repo.load_pricing_evidence(run_id)
        else:
            stored = {
                "schema_version": 1,
                "run_id": run_id,
                "items": {},
            }
        items = stored.get("items")
        if not isinstance(items, dict):
            items = {}
        confirmed_item = {
            **calculated.data,
            "status": "confirmed",
            "confirmed_by": "workbench_user",
            "confirmed_at": utc_now_iso(),
        }
        items[seed_id] = confirmed_item
        stored.update(
            {
                "schema_version": 1,
                "run_id": run_id,
                "items": items,
            }
        )
        self.repo.save_pricing_evidence(run_id, stored)
        self.repo.append_run_event(
            run_id,
            "pricing_evidence.confirmed",
            "User confirmed product cost, package evidence, and listing price.",
            {
                "seed_id": seed_id,
                "listing_price_cny": confirmed_item["calculation"][
                    "listing_price_cny"
                ],
                "listing_price_rub": confirmed_item["calculation"][
                    "listing_price_rub"
                ],
            },
        )
        return Result.success(
            "pricing_evidence.confirmed",
            "Product price and package evidence confirmed.",
            confirmed_item,
        )

    def _calculate_pricing_evidence(
        self,
        run_id: str,
        payload: dict[str, Any],
        *,
        confirmed: bool,
    ) -> Result:
        self.repo.load_run(run_id)
        seed_id = str(payload.get("seed_id") or "").strip()
        if not seed_id:
            return Result.failure(
                "pricing_evidence.seed_required",
                "A product seed_id is required.",
                data={"run_id": run_id},
            )

        try:
            ozon_result = self.repo.load_ozon_collection_result(run_id)
        except FileNotFoundError:
            return Result.failure(
                "pricing_evidence.ozon_evidence_required",
                "Ozon product evidence is required before price calculation.",
                data={"run_id": run_id, "seed_id": seed_id},
            )
        candidate = next(
            (
                item
                for item in ozon_result.get("ozon_candidates", [])
                if isinstance(item, dict)
                and str(item.get("seed_id") or "") == seed_id
            ),
            None,
        )
        if candidate is None:
            return Result.failure(
                "pricing_evidence.product_not_found",
                "The requested product does not belong to this batch.",
                data={"run_id": run_id, "seed_id": seed_id},
            )

        selection_path = (
            self.repo.run_dir(run_id) / "supplier_sku_selections.json"
        )
        selections: dict[str, Any] = {}
        if selection_path.exists():
            loaded = self.repo.load_supplier_sku_selections(run_id)
            raw_selections = loaded.get("selections")
            if isinstance(raw_selections, dict):
                selections = raw_selections
        selection = selections.get(seed_id)
        supplier_sku = (
            selection.get("supplier_sku")
            if isinstance(selection, dict)
            else None
        )
        if not isinstance(supplier_sku, dict) or not supplier_sku:
            return Result.failure(
                "pricing_evidence.supplier_sku_required",
                "Lock one real 1688 SKU before confirming cost and package evidence.",
                data={"run_id": run_id, "seed_id": seed_id},
            )

        supplier_product: dict[str, Any] = {}
        supplier_path = (
            self.repo.run_dir(run_id) / "supplier_collection_result.json"
        )
        if supplier_path.exists():
            supplier_result = self.repo.load_supplier_collection_result(run_id)
            supplier_product = next(
                (
                    item
                    for item in supplier_result.get(
                        "supplier_products", []
                    )
                    if isinstance(item, dict)
                    and str(item.get("seed_id") or "") == seed_id
                ),
                {},
            )

        try:
            inputs = PricingInput.from_values(
                purchase_price_cny=payload.get("purchase_price_cny"),
                domestic_shipping_cny=payload.get(
                    "domestic_shipping_cny"
                ),
                package_weight_g=payload.get("package_weight_g"),
                package_length_cm=payload.get("package_length_cm"),
                package_width_cm=payload.get("package_width_cm"),
                package_height_cm=payload.get("package_height_cm"),
                target_margin_rate=payload.get("target_margin_rate"),
            )
            policy = PricingPolicy.from_mapping(
                self.repo.load_pricing_settings()
            )
            quote = calculate_listing_price(
                inputs,
                policy,
                initial_sale_rub=_candidate_sale_rub(candidate),
            )
        except ValueError as exc:
            return Result.failure(
                "pricing_evidence.calculation_invalid",
                "The pricing inputs or fixed pricing policy are invalid.",
                data={"run_id": run_id, "seed_id": seed_id},
                errors=[str(exc)],
            )

        supplier_offer_id = str(
            (selection or {}).get("supplier_offer_id")
            or supplier_product.get("offer_id")
            or ""
        ).strip()
        data = {
            "seed_id": seed_id,
            "status": "confirmed" if confirmed else "preview",
            "inputs": inputs.to_dict(),
            "parameter_snapshot": policy.to_dict(),
            "calculation": quote.to_dict(),
            "supplier_offer_id": supplier_offer_id or None,
            "supplier_url": _supplier_product_url(
                supplier_product,
                supplier_offer_id,
            ),
            "supplier_sku_id": (
                str(supplier_sku.get("supplier_sku_id") or "").strip()
                or None
            ),
            "supplier_reference_price": supplier_sku.get("price"),
            "ozon_reference_price_rub": str(
                _candidate_sale_rub(candidate)
            ),
        }
        return Result.success(
            (
                "pricing_evidence.confirmation_calculated"
                if confirmed
                else "pricing_evidence.previewed"
            ),
            "Product listing price calculated from confirmed cost and package inputs.",
            data,
        )

    def upload_workspace(self, run_id: str) -> Result:
        run = self.repo.load_run(run_id)
        try:
            template_result = self.repo.load_attribute_template_result(run_id)
            ozon_result = self.repo.load_ozon_collection_result(run_id)
        except FileNotFoundError:
            return Result.failure(
                "upload_workspace.source_missing",
                "Category template and Ozon evidence are required before a draft can be prepared.",
                data={"run_id": run_id, "status": run["status"]},
            )

        templates_by_seed = {
            str(item.get("seed_id") or ""): item
            for item in template_result.get("seed_templates", [])
            if isinstance(item, dict)
        }
        suppliers_by_seed: dict[str, dict[str, Any]] = {}
        supplier_result_path = self.repo.run_dir(run_id) / "supplier_collection_result.json"
        if supplier_result_path.exists():
            supplier_result = self.repo.load_supplier_collection_result(run_id)
            suppliers_by_seed = {
                str(item.get("seed_id") or ""): item
                for item in supplier_result.get("supplier_products", [])
                if isinstance(item, dict)
            }
        supplier_selections: dict[str, dict[str, Any]] = {}
        selection_path = self.repo.run_dir(run_id) / "supplier_sku_selections.json"
        if selection_path.exists():
            raw_selections = self.repo.load_supplier_sku_selections(run_id).get("selections", {})
            if isinstance(raw_selections, dict):
                supplier_selections = {
                    str(seed_id): selection
                    for seed_id, selection in raw_selections.items()
                    if isinstance(selection, dict)
                }
        generated_content_items: dict[str, dict[str, Any]] = {}
        generated_content_path = self.repo.run_dir(run_id) / "generated_content_result.json"
        if generated_content_path.exists():
            raw_generated_content = self.repo.load_generated_content_result(run_id).get("items", {})
            if isinstance(raw_generated_content, dict):
                generated_content_items = {
                    str(seed_id): content
                    for seed_id, content in raw_generated_content.items()
                    if isinstance(content, dict)
                }
        subject_path = self.repo.run_dir(run_id) / "subject_masters.json"
        subject_store = (
            self.repo.load_subject_masters(run_id)
            if subject_path.exists()
            else {"schema_version": 1, "run_id": run_id, "items": {}}
        )
        subject_items = subject_store.get("items", {})
        subject_items_changed = False
        repaired_subject_seed_ids: set[str] = set()
        pricing_policy = PricingPolicy.from_mapping(
            self.repo.load_pricing_settings()
        ).to_dict()
        pricing_items: dict[str, dict[str, Any]] = {}
        pricing_path = self.repo.run_dir(run_id) / "pricing_evidence.json"
        if pricing_path.exists():
            raw_pricing_items = self.repo.load_pricing_evidence(run_id).get(
                "items", {}
            )
            if isinstance(raw_pricing_items, dict):
                pricing_items = {
                    str(seed_id): item
                    for seed_id, item in raw_pricing_items.items()
                    if isinstance(item, dict)
                }
        required_attribute_evidence: dict[str, dict[str, Any]] = {}
        required_evidence_path = (
            self.repo.run_dir(run_id) / "required_attribute_evidence.json"
        )
        if required_evidence_path.exists():
            raw_required_evidence = self.repo.load_required_attribute_evidence(
                run_id
            ).get("items", {})
            if isinstance(raw_required_evidence, dict):
                required_attribute_evidence = {
                    str(seed_id): item
                    for seed_id, item in raw_required_evidence.items()
                    if isinstance(item, dict)
                }
        upload_previews: dict[str, dict[str, Any]] = {}
        preview_path = self.repo.run_dir(run_id) / "upload_previews.json"
        if preview_path.exists():
            raw_previews = self.repo.load_upload_previews(run_id).get("items", {})
            if isinstance(raw_previews, dict):
                upload_previews = {
                    str(seed_id): item
                    for seed_id, item in raw_previews.items()
                    if isinstance(item, dict)
                }
        upload_submissions: dict[str, dict[str, Any]] = {}
        submission_path = self.repo.run_dir(run_id) / "upload_submissions.json"
        if submission_path.exists():
            raw_submissions = self.repo.load_upload_submissions(run_id).get(
                "items", {}
            )
            if isinstance(raw_submissions, dict):
                upload_submissions = {
                    str(seed_id): item
                    for seed_id, item in raw_submissions.items()
                    if isinstance(item, dict)
                }
        submissions_changed = False
        for submitted_seed_id, submission in upload_submissions.items():
            if submission.get("task_id") is None:
                continue
            before = json.dumps(
                submission,
                ensure_ascii=False,
                sort_keys=True,
            )
            preview = upload_previews.get(submitted_seed_id)
            self._sync_image_task_record(
                run_id=run_id,
                seed_id=submitted_seed_id,
                record=submission,
                preview=preview if isinstance(preview, dict) else None,
            )
            submissions_changed = submissions_changed or before != json.dumps(
                submission,
                ensure_ascii=False,
                sort_keys=True,
            )
        if submissions_changed:
            self.repo.save_upload_submissions(
                run_id,
                {
                    "schema_version": 1,
                    "run_id": run_id,
                    "items": upload_submissions,
                },
            )
        candidates = ozon_result.get("ozon_candidates") if isinstance(ozon_result.get("ozon_candidates"), list) else []
        items: list[dict[str, Any]] = []
        for candidate in candidates:
            seed_id = str(candidate.get("seed_id") or "")
            template = templates_by_seed.get(seed_id, {})
            template_binding_errors = self._attribute_template_product_binding_errors(
                run_id,
                {"seed_templates": [template] if template else []},
                [seed_id],
            )
            raw_schema = (
                template.get("upload_attribute_schema")
                if isinstance(template.get("upload_attribute_schema"), list)
                else []
            )
            schema = _effective_upload_schema(raw_schema)
            prefill_plan = template.get("draft_prefill_plan") if isinstance(template.get("draft_prefill_plan"), list) else []
            seller_template = template.get("seller_attribute_template") or {}
            category_candidates = template.get("category_candidates") if isinstance(template.get("category_candidates"), list) else []
            supplier_product = suppliers_by_seed.get(seed_id) or {}
            supplier_source_images = self._supplier_product_images(supplier_product)
            supplier_selection = supplier_selections.get(seed_id) or {}
            supplier_sku = supplier_selection.get("supplier_sku") or {}
            supplier_offer_id = (
                supplier_selection.get("supplier_offer_id")
                or supplier_product.get("offer_id")
            )
            pricing_prefill = _pricing_prefill_from_evidence(
                candidate=candidate,
                supplier_product=supplier_product,
                supplier_sku=supplier_sku,
            )
            pricing_record = pricing_items.get(seed_id) or {}
            pricing_confirmed = (
                pricing_record.get("status") == "confirmed"
            )
            pricing_policy_current = (
                pricing_record.get("parameter_snapshot") == pricing_policy
            )
            pricing_validation_errors = (
                _pricing_record_validation_errors(pricing_record)
                if pricing_confirmed
                else []
            )
            pricing_ready = bool(
                pricing_confirmed
                and pricing_policy_current
                and not pricing_validation_errors
            )
            pricing_status = (
                "confirmed"
                if pricing_ready
                else "invalid"
                if pricing_validation_errors
                else "stale"
                if pricing_confirmed
                else "missing"
            )
            upload_core_fields = (
                _pricing_upload_core_fields(pricing_record)
                if pricing_ready
                else None
            )
            media = candidate.get("selected_sku_media") or {}
            images = media.get("selected_sku_images") or media.get("main_gallery_images") or []
            subject_entry = subject_items.get(seed_id) if isinstance(subject_items, dict) else None
            subject_master = (
                subject_entry.get("subject_master")
                if isinstance(subject_entry, dict)
                and isinstance(subject_entry.get("subject_master"), dict)
                else {}
            )
            subject_master, subject_repaired = (
                self._normalize_subject_master_to_locked_sku(
                    subject_master,
                    supplier_selection,
                )
            )
            if subject_repaired and isinstance(subject_entry, dict):
                subject_entry["subject_master"] = subject_master
                subject_items_changed = True
                repaired_subject_seed_ids.add(seed_id)
                preview = upload_previews.get(seed_id)
                if isinstance(preview, dict):
                    preview.setdefault("image_task_context", {})[
                        "subject_master"
                    ] = subject_master
                    preview.setdefault("bootstrap_image", {})["url"] = (
                        subject_master.get("source_image_url")
                    )
            subject_image_urls = [
                str(value).strip()
                for value in (
                    subject_master.get("source_image_urls")
                    or [subject_master.get("source_image_url")]
                )
                if str(value or "").strip()
            ]
            bootstrap_image_url = (
                subject_image_urls[0] if subject_image_urls else ""
            )
            expected_subject_product_ids = {
                seed_id,
                str(candidate.get("ozon_product_id") or "").strip(),
            }
            expected_subject_product_ids.discard("")
            bootstrap_image_ready = bool(
                bootstrap_image_url
                and _is_public_https_url(bootstrap_image_url)
                and str(subject_master.get("run_id") or "") == run_id
                and str(subject_master.get("product_id") or "")
                in expected_subject_product_ids
                and str(subject_master.get("subject_master_sha256") or "")
            )
            image_job_id = (
                str(subject_entry.get("image_job_id") or "")
                if isinstance(subject_entry, dict)
                else ""
            )
            image_job = None
            if image_job_id:
                try:
                    image_job = self._image_job_payload(image_job_id)
                except ValueError:
                    image_job = None
            accepted_slots = [
                slot
                for slot in (image_job or {}).get("slots", [])
                if slot.get("status") == "accepted"
                and str(slot.get("accepted_path") or "")
                and Path(str(slot["accepted_path"])).is_file()
            ]
            image_generation_status = str((image_job or {}).get("status") or "not_queued")
            generated_images_ready = (
                image_generation_status == "completed"
                and len(accepted_slots) == 8
            )
            category_candidate = category_candidates[0] if category_candidates else {}
            product_type = next(
                (
                    value
                    for key, value in (candidate.get("attributes") or {}).items()
                    if str(key).strip().casefold() in {"тип", "type"}
                    and str(value or "").strip()
                ),
                "",
            )
            category_assessment = assess_category_template_match(
                category_path=str(
                    candidate.get("category_path")
                    or category_candidate.get("category_path")
                    or ""
                ),
                leaf_category=str(
                    candidate.get("leaf_category")
                    or category_candidate.get("leaf_category")
                    or ""
                ),
                matched_category_path=str(seller_template.get("matched_category_path") or ""),
                product_title=str(candidate.get("title") or ""),
                product_type=str(product_type or ""),
                category_url=str(
                    candidate.get("category_url")
                    or category_candidate.get("category_url")
                    or ""
                ),
            )
            if template_binding_errors:
                category_assessment = {
                    **category_assessment,
                    "credible": False,
                    "reason": "candidate_revision_or_product_binding_mismatch",
                    "binding_errors": template_binding_errors,
                }
            generated_field_results = (
                (generated_content_items.get(seed_id) or {}).get(
                    "field_results"
                )
                or (generated_content_items.get(seed_id) or {}).get("fields")
                or {}
            )
            incompatible_required_fields = _required_not_applicable_fields(
                schema,
                generated_field_results,
            )
            if incompatible_required_fields:
                category_assessment = {
                    **category_assessment,
                    "credible": False,
                    "reason": "required_template_fields_not_applicable",
                    "incompatible_required_fields": (
                        incompatible_required_fields
                    ),
                }
            template_ready = bool(
                schema
                and seller_template
                and category_assessment["credible"]
            )
            mapping = (
                map_template_attributes(
                    schema,
                    candidate,
                    supplier_product=suppliers_by_seed.get(seed_id),
                    supplier_selection=supplier_selections.get(seed_id),
                    pricing_evidence=(
                        pricing_record.get("inputs")
                        if pricing_ready
                        and isinstance(pricing_record.get("inputs"), dict)
                        else None
                    ),
                    rewritten_content=(
                        generated_field_results
                    ),
                    user_confirmed_required_fields=(
                        (required_attribute_evidence.get(seed_id) or {}).get(
                            "values"
                        )
                        or {}
                    ),
                )
                if template_ready
                else {
                    "fields": [],
                    "mapped_attribute_count": 0,
                    "rewrite_required_count": 0,
                    "missing_fact_count": 0,
                    "not_applicable_count": 0,
                    "excluded_attribute_count": 0,
                    "required_attribute_count": 0,
                    "required_mapped_count": 0,
                    "missing_required_fields": [],
                    "skill_pending_required_fields": [],
                    "manual_required_fields": [],
                    "required_attributes_ready": False,
                    "attribute_score_progress": attribute_content_score_progress([]),
                }
            )
            required_attributes_ready = bool(
                template_ready and mapping["required_attributes_ready"]
            )
            original_content_ready = bool(
                template_ready and mapping["rewrite_required_count"] == 0
            )
            blocking_gates: list[str] = []
            if not template_ready:
                blocking_gates.append("category_template")
            elif not required_attributes_ready:
                blocking_gates.append("required_attributes")
            if template_ready and not original_content_ready:
                blocking_gates.append("original_content")
            if not bootstrap_image_ready:
                blocking_gates.append("bootstrap_image")
            if not pricing_ready:
                blocking_gates.append("pricing")
            items.append(
                {
                    "seed_id": seed_id,
                    "source_title": candidate.get("title"),
                    "source_description": (candidate.get("content_score_evidence") or {}).get("description_or_rich_content_blocks") or [],
                    "source_attributes": candidate.get("attributes") or {},
                    "source_content_score_evidence": candidate.get("content_score_evidence") or {},
                    "supplier_attributes": supplier_product.get("attributes") or {},
                    "supplier_title": supplier_product.get("title"),
                    "supplier_offer_id": supplier_offer_id,
                    "supplier_url": _supplier_product_url(
                        supplier_product,
                        str(supplier_offer_id or ""),
                    ),
                    "supplier_selected_sku": (
                        supplier_sku
                    ),
                    "supplier_source_images": supplier_source_images,
                    "subject_master": subject_master,
                    "bootstrap_image_url": bootstrap_image_url or None,
                    "bootstrap_image_ready": bootstrap_image_ready,
                    "supplier_reference_price": supplier_sku.get("price"),
                    "supplier_selected_options": (
                        ((supplier_selections.get(seed_id) or {}).get("supplier_sku") or {}).get("selected_options")
                        or {}
                    ),
                    "source_image": images[0] if images else None,
                    "ozon_reference_images": list(images),
                    "selected_options": (
                        (
                            (supplier_selections.get(seed_id) or {}).get(
                                "ozon_target_sku"
                            )
                            or {}
                        ).get("selected_options")
                        or (candidate.get("target_sku") or {}).get(
                            "selected_options"
                        )
                        or {}
                    ),
                    "category_path": seller_template.get("matched_category_path") or (category_candidates[0].get("category_path") if category_candidates else None),
                    "description_category_id": seller_template.get("description_category_id"),
                    "type_id": seller_template.get("type_id"),
                    "required_attribute_count": mapping["required_attribute_count"],
                    "attribute_schema_count": len(schema),
                    "prefill_plan_count": len(prefill_plan),
                    "prefill_plan": prefill_plan,
                    "video_template_fields": _video_template_fields(schema),
                    "template_ready": template_ready,
                    "category_template_assessment": category_assessment,
                    "attribute_mapping": mapping["fields"],
                    "mapped_attribute_count": mapping["mapped_attribute_count"],
                    "rewrite_required_count": mapping["rewrite_required_count"],
                    "missing_fact_count": mapping["missing_fact_count"],
                    "not_applicable_count": mapping["not_applicable_count"],
                    "excluded_attribute_count": mapping["excluded_attribute_count"],
                    "required_mapped_count": mapping["required_mapped_count"],
                    "missing_required_fields": mapping["missing_required_fields"],
                    "skill_pending_required_fields": mapping[
                        "skill_pending_required_fields"
                    ],
                    "manual_required_fields": mapping["manual_required_fields"],
                    "attribute_score_progress": mapping[
                        "attribute_score_progress"
                    ],
                    "required_attributes_ready": required_attributes_ready,
                    "original_content_ready": original_content_ready,
                    "generated_images_ready": generated_images_ready,
                    "pricing_ready": pricing_ready,
                    "pricing_status": pricing_status,
                    "pricing_validation_errors": pricing_validation_errors,
                    "pricing_evidence": (
                        pricing_record if pricing_record else None
                    ),
                    "pricing_prefill": pricing_prefill,
                    "upload_core_fields": upload_core_fields,
                    "pricing_policy": pricing_policy,
                    "blocking_gates": blocking_gates,
                    "ready_to_build": not blocking_gates,
                    "image_job_id": image_job_id or None,
                    "image_generation_status": image_generation_status,
                    "generated_image_count": len(accepted_slots),
                    "generated_image_url": (
                        f"/api/batches/{run_id}/image-job/{image_job_id}/slot/"
                        f"{accepted_slots[0]['slot_id']}/file"
                        if accepted_slots
                        else None
                    ),
                    "generated_image_urls": [
                        f"/api/batches/{run_id}/image-job/{image_job_id}/slot/"
                        f"{slot['slot_id']}/file"
                        for slot in accepted_slots
                    ],
                    "generated_image_files": [
                        {
                            "slot_id": str(slot["slot_id"]),
                            "path": str(Path(str(slot["accepted_path"])).resolve()),
                        }
                        for slot in accepted_slots
                    ],
                    "upload_preview": (
                        upload_previews.get(seed_id)
                        if not blocking_gates
                        else None
                    ),
                    "upload_submission": upload_submissions.get(seed_id),
                }
            )

        if subject_items_changed:
            self.repo.save_subject_masters(
                run_id,
                {
                    **subject_store,
                    "items": subject_items,
                    "updated_at": utc_now_iso(),
                },
            )
            if upload_previews:
                self.repo.save_upload_previews(
                    run_id,
                    {
                        "schema_version": 1,
                        "run_id": run_id,
                        "items": upload_previews,
                    },
                )
            for repaired_seed_id in repaired_subject_seed_ids:
                submission = upload_submissions.get(repaired_seed_id)
                subject_entry = subject_items.get(repaired_seed_id)
                subject_master = (
                    subject_entry.get("subject_master")
                    if isinstance(subject_entry, dict)
                    else None
                )
                supplier_selection = supplier_selections.get(repaired_seed_id) or {}
                if isinstance(submission, dict) and isinstance(subject_master, dict):
                    self._refresh_pending_image_task_subject(
                        package_id=str(
                            submission.get("image_task_package_id") or ""
                        ),
                        run_id=run_id,
                        seed_id=repaired_seed_id,
                        subject_master=subject_master,
                        locked_supplier_sku=(
                            supplier_selection.get("supplier_sku") or {}
                        ),
                    )

        template_ready = bool(items) and all(item["template_ready"] for item in items)
        required_attributes_ready = bool(items) and all(
            item["required_attributes_ready"] for item in items
        )
        original_content_ready = bool(items) and all(
            item["original_content_ready"] for item in items
        )
        images_ready = bool(items) and all(
            item["bootstrap_image_ready"] for item in items
        )
        pricing_ready = bool(items) and all(
            item["pricing_ready"] for item in items
        )
        pricing_ready_count = sum(
            1 for item in items if item["pricing_ready"]
        )
        generated_image_count = sum(item["generated_image_count"] for item in items)
        generated_product_count = sum(
            1 for item in items if item["generated_image_count"] == 8
        )
        approved_product_count = sum(
            1 for item in items if item["generated_images_ready"]
        )
        bootstrap_image_ready_count = sum(
            1 for item in items if item["bootstrap_image_ready"]
        )
        template_ready_count = sum(1 for item in items if item["template_ready"])
        required_attributes_ready_count = sum(
            1 for item in items if item["required_attributes_ready"]
        )
        original_content_ready_count = sum(
            1 for item in items if item["original_content_ready"]
        )
        mapped_attribute_count = sum(item["mapped_attribute_count"] for item in items)
        rewrite_required_count = sum(item["rewrite_required_count"] for item in items)
        missing_fact_count = sum(item["missing_fact_count"] for item in items)
        not_applicable_count = sum(item["not_applicable_count"] for item in items)
        required_mapped_count = sum(item["required_mapped_count"] for item in items)
        valid_required_attribute_count = sum(
            item["required_attribute_count"]
            for item in items
            if item["template_ready"]
        )
        ready_to_build_count = sum(1 for item in items if item["ready_to_build"])
        active_seed_ids = {str(item.get("seed_id") or "") for item in items}
        active_submissions = [
            submission
            for submitted_seed_id, submission in upload_submissions.items()
            if submitted_seed_id in active_seed_ids
        ]
        submission_attempted_count = len(active_submissions)
        submission_accepted_count = sum(
            1
            for submission in active_submissions
            if str(submission.get("status") or "") == "accepted_by_ozon"
        )
        submission_failed_count = sum(
            1
            for submission in active_submissions
            if str(submission.get("status") or "").casefold()
            in {"failed", "error", "declined"}
        )
        image_task_package_count = sum(
            1
            for submission in active_submissions
            if str(submission.get("image_task_package_id") or "").strip()
            and str(submission.get("image_task_package_status") or "").strip()
            in {
                "pending",
                "in_progress",
                "grid_ready",
                "awaiting_product",
                "completed",
                "failed",
            }
            and not submission.get("image_task_package_error")
        )
        image_task_waiting_binding_count = sum(
            1
            for submission in active_submissions
            if str(submission.get("image_task_package_status") or "")
            == "awaiting_product"
        )
        image_task_missing_count = max(
            0,
            submission_attempted_count - image_task_package_count,
        )
        all_products_ready = bool(items) and ready_to_build_count == len(items)
        draft_ready = run.get("status") in {
            WorkbenchState.DRAFT_READY.value,
            WorkbenchState.PUBLISH_WAITING_CONFIRMATION.value,
            WorkbenchState.PUBLISH_SUBMITTED.value,
            WorkbenchState.DONE.value,
        }
        return Result.success(
            "upload_workspace.loaded",
            "Upload draft workspace loaded.",
            {
                "run_id": run_id,
                "status": run["status"],
                "items": items,
                "gates": {
                    "category_template_ready": template_ready,
                    "required_attributes_ready": required_attributes_ready,
                    "original_content_ready": original_content_ready,
                    "images_ready": images_ready,
                    "bootstrap_images_ready": images_ready,
                    "pricing_ready": pricing_ready,
                    "pricing_ready_count": pricing_ready_count,
                    "pricing_pending_count": len(items) - pricing_ready_count,
                    "pricing_policy": pricing_policy,
                    "generated_image_count": generated_image_count,
                    "generated_product_count": generated_product_count,
                    "approved_product_count": approved_product_count,
                    "bootstrap_image_ready_count": (
                        bootstrap_image_ready_count
                    ),
                    "template_ready_count": template_ready_count,
                    "required_attributes_ready_count": required_attributes_ready_count,
                    "original_content_ready_count": original_content_ready_count,
                    "mapped_attribute_count": mapped_attribute_count,
                    "rewrite_required_count": rewrite_required_count,
                    "missing_fact_count": missing_fact_count,
                    "not_applicable_count": not_applicable_count,
                    "required_mapped_count": required_mapped_count,
                    "valid_required_attribute_count": valid_required_attribute_count,
                    "ready_to_build_count": ready_to_build_count,
                    "blocked_product_count": len(items) - ready_to_build_count,
                    "submission_attempted_count": submission_attempted_count,
                    "submission_accepted_count": submission_accepted_count,
                    "submission_failed_count": submission_failed_count,
                    "image_task_package_count": image_task_package_count,
                    "image_task_waiting_binding_count": (
                        image_task_waiting_binding_count
                    ),
                    "image_task_missing_count": image_task_missing_count,
                    "all_products_ready": all_products_ready,
                    "product_count": len(items),
                    "draft_ready": draft_ready,
                    "publish_locked": bool(run.get("publish_locked", True)),
                    "ready_to_build": ready_to_build_count > 0,
                },
            },
        )

    def save_required_attribute_evidence(
        self,
        run_id: str,
        seed_id: str,
        values: dict[str, Any],
    ) -> Result:
        workspace = self.upload_workspace(run_id)
        if not workspace.ok:
            return workspace
        item = next(
            (
                candidate
                for candidate in workspace.data.get("items", [])
                if str(candidate.get("seed_id") or "") == seed_id
            ),
            None,
        )
        if item is None:
            return Result.failure(
                "required_attribute_evidence.product_missing",
                "The requested product is not part of this batch.",
                data={"run_id": run_id, "seed_id": seed_id},
            )
        if item.get("template_ready") is not True:
            return Result.failure(
                "required_attribute_evidence.template_not_ready",
                "Resolve the Seller API category template before filling required fields.",
                data={"run_id": run_id, "seed_id": seed_id},
            )
        missing_fields = {
            str(field.get("field_key") or ""): field
            for field in item.get("manual_required_fields", [])
            if str(field.get("field_key") or "")
        }
        normalized_values = {
            str(field_key).strip(): value
            for field_key, value in (values or {}).items()
            if str(field_key).strip()
        }
        normalized_values = {
            field_key: _normalized_required_attribute_value(
                missing_fields.get(field_key),
                value,
            )
            for field_key, value in normalized_values.items()
        }
        invalid_keys = sorted(set(normalized_values) - set(missing_fields))
        empty_keys = sorted(
            field_key
            for field_key, value in normalized_values.items()
            if not _has_content_value(value)
        )
        if not normalized_values or invalid_keys or empty_keys:
            return Result.failure(
                "required_attribute_evidence.invalid_fields",
                "Only required fields confirmed unresolved by the field Skill can be saved, and every value must be non-empty.",
                errors=[
                    *(
                        ["Not currently missing required: " + ", ".join(invalid_keys)]
                        if invalid_keys
                        else []
                    ),
                    *(
                        ["Empty required values: " + ", ".join(empty_keys)]
                        if empty_keys
                        else []
                    ),
                ],
                data={
                    "run_id": run_id,
                    "seed_id": seed_id,
                    "allowed_field_keys": sorted(missing_fields),
                },
            )
        for field_key, value in normalized_values.items():
            allowed_values = missing_fields[field_key].get("allowed_values")
            if (
                isinstance(allowed_values, list)
                and allowed_values
                and value not in allowed_values
                and str(value) not in {str(candidate) for candidate in allowed_values}
            ):
                return Result.failure(
                    "required_attribute_evidence.dictionary_value_invalid",
                    "Choose a valid Seller API dictionary value for the required field.",
                    data={
                        "run_id": run_id,
                        "seed_id": seed_id,
                        "field_key": field_key,
                        "allowed_values": allowed_values,
                    },
                )
        payload = {
            "schema_version": 1,
            "run_id": run_id,
            "items": {},
        }
        path = self.repo.run_dir(run_id) / "required_attribute_evidence.json"
        if path.exists():
            saved = self.repo.load_required_attribute_evidence(run_id)
            if isinstance(saved, dict):
                payload.update(saved)
        if not isinstance(payload.get("items"), dict):
            payload["items"] = {}
        existing = payload["items"].get(seed_id)
        existing_values = (
            dict(existing.get("values") or {})
            if isinstance(existing, dict)
            else {}
        )
        existing_values.update(
            {
                field_key: {
                    "value": value,
                    "label": missing_fields[field_key].get("label"),
                    "source": "user_confirmed_required_attribute",
                    "confirmed_at": utc_now_iso(),
                }
                for field_key, value in normalized_values.items()
            }
        )
        payload["items"][seed_id] = {
            "description_category_id": item.get("description_category_id"),
            "type_id": item.get("type_id"),
            "values": existing_values,
            "updated_at": utc_now_iso(),
        }
        saved_path = self.repo.save_required_attribute_evidence(
            run_id,
            payload,
        )
        refreshed = self.upload_workspace(run_id)
        refreshed_item = next(
            (
                candidate
                for candidate in refreshed.data.get("items", [])
                if str(candidate.get("seed_id") or "") == seed_id
            ),
            {},
        )
        return Result.success(
            "required_attribute_evidence.saved",
            "User-confirmed values were saved for this product's missing required fields.",
            {
                "run_id": run_id,
                "seed_id": seed_id,
                "saved_field_keys": sorted(normalized_values),
                "missing_required_fields": refreshed_item.get(
                    "missing_required_fields",
                    [],
                ),
                "required_attributes_ready": refreshed_item.get(
                    "required_attributes_ready",
                    False,
                ),
                "evidence_path": str(saved_path),
            },
        )

    def content_tasks(self, run_id: str) -> Result:
        workspace = self.upload_workspace(run_id)
        if not workspace.ok:
            return workspace
        generated_content_path = self.repo.run_dir(run_id) / "generated_content_result.json"
        generated_items: dict[str, dict[str, Any]] = {}
        if generated_content_path.exists():
            raw_items = self.repo.load_generated_content_result(run_id).get("items", {})
            if isinstance(raw_items, dict):
                generated_items = {
                    str(seed_id): item
                    for seed_id, item in raw_items.items()
                    if isinstance(item, dict)
                }

        task_items: list[dict[str, Any]] = []
        for item in workspace.data.get("items", []):
            if item.get("template_ready") is not True:
                continue
            intelligent_fields = [
                field
                for field in item.get("attribute_mapping", [])
                if field.get("required_reason") != "ozon_compliance_decision"
                and (
                    field.get("status") in {"rewrite_required", "missing_fact"}
                    or field.get("source")
                    in {
                        "generated_original_content",
                        "generated_evidence_completion",
                    }
                    or field.get("intelligence_decision") == "unresolved"
                )
            ]
            if not intelligent_fields:
                continue
            seed_id = str(item.get("seed_id") or "")
            generated = generated_items.get(seed_id) or {}
            generated_field_results = _generated_field_results(generated)
            evidence = {
                "ozon_title_style_reference": item.get("source_title"),
                "ozon_attributes": item.get("source_attributes") or {},
                "ozon_selected_sku": item.get("selected_options") or {},
                "ozon_content_score_evidence": item.get("source_content_score_evidence") or {},
                "supplier_title": item.get("supplier_title"),
                "supplier_attributes": item.get("supplier_attributes") or {},
                "supplier_offer_id": item.get("supplier_offer_id"),
                "confirmed_supplier_sku": item.get("supplier_selected_sku") or {},
                "supplier_visual_evidence": {
                    "locked_sku_images": (
                        (item.get("supplier_selected_sku") or {}).get(
                            "image_urls"
                        )
                        or []
                    ),
                    "product_images": item.get("supplier_source_images") or [],
                    "page_single_sku": (
                        (item.get("supplier_selected_sku") or {}).get(
                            "evidence_source"
                        )
                        == "single_sku_detail_page"
                    ),
                },
            }
            evidence_index = _content_evidence_index(evidence)
            supplier_truth_available = bool(
                evidence["supplier_attributes"] or evidence["confirmed_supplier_sku"]
            )
            field_tasks: list[dict[str, Any]] = []
            for field in intelligent_fields:
                field_key = str(field.get("field_key") or "")
                canonical_label = canonical_attribute_label(field.get("label"))
                mode = (
                    "creative_rewrite"
                    if canonical_label
                    in {"title", "description", "rich_content", "hashtags"}
                    else "evidence_inference"
                )
                stored_result = generated_field_results.get(field_key)
                visual_evidence_refs = _field_visual_evidence_refs(
                    field,
                    evidence_index,
                    evidence,
                )
                completed_result = (
                    stored_result
                    if _content_result_is_accepted(
                        field,
                        stored_result,
                        visual_evidence_refs=visual_evidence_refs,
                    )
                    else None
                )
                field_tasks.append(
                    {
                        "field_key": field_key,
                        "label": field.get("label"),
                        "mode": mode,
                        "status": (
                            "completed"
                            if completed_result is not None
                            else "pending"
                        ),
                        "decision": (
                            completed_result.get("decision")
                            if completed_result
                            else None
                        ),
                        "required": field.get("required") is True,
                        "attribute_type": field.get("attribute_type"),
                        "dictionary_id": field.get("dictionary_id"),
                        "allowed_values": field.get("allowed_values") or [],
                        "visual_inference_supported": bool(
                            field.get("visual_inference_supported")
                        ),
                        "visual_evidence_refs": visual_evidence_refs,
                        "visual_evidence_roles": (
                            _visual_evidence_roles(visual_evidence_refs)
                        ),
                        "visual_target_scope": _visual_target_scope(field),
                        "visual_evidence_policy": (
                            "Identify the locked SKU's primary product subject "
                            "before deciding the field. Exclude accessories, "
                            "packaging, backgrounds, overlays, and unrelated "
                            "reference variants from primary-subject facts. "
                            "Generated images are never product-fact evidence."
                            if visual_evidence_refs
                            else None
                        ),
                        "current_mapping_status": field.get("status"),
                        "reference_evidence": field.get("reference_evidence", []),
                        "candidate_evidence_refs": _field_candidate_evidence_refs(
                            field,
                            evidence_index,
                        ),
                        "supplier_truth_required": bool(
                            supplier_truth_available
                            and canonical_label in {"brand", "model", "article", "color"}
                        ),
                    }
                )
            pending_field_count = sum(
                1 for field in field_tasks if field["status"] == "pending"
            )
            unresolved_field_count = sum(
                1 for field in field_tasks if field.get("decision") == "unresolved"
            )
            blocking_unresolved_field_count = sum(
                1
                for field in field_tasks
                if field.get("decision") == "unresolved"
                and field.get("required") is True
            )
            if pending_field_count:
                task_status = "pending"
            elif blocking_unresolved_field_count:
                task_status = "blocked"
            elif unresolved_field_count:
                task_status = "completed_with_gaps"
            else:
                task_status = "ready"
            task_items.append(
                {
                    "seed_id": seed_id,
                    "status": task_status,
                    "target_score": 90,
                    "category_path": item.get("category_path"),
                    "field_tasks": field_tasks,
                    "rewrite_fields": [
                        {
                            "field_key": field.get("field_key"),
                            "label": field.get("label"),
                            "attribute_type": field.get("attribute_type"),
                            "reference_evidence": field.get("reference_evidence", []),
                        }
                        for field in intelligent_fields
                        if canonical_attribute_label(field.get("label"))
                        in {"title", "description", "rich_content", "hashtags"}
                    ],
                    "evidence": evidence,
                    "evidence_index": evidence_index,
                    "rules": {
                        "language": "ru-RU",
                        "use_ozon_structure_as_reference": True,
                        "copy_ozon_text_verbatim": False,
                        "supplier_truth_overrides_ozon": True,
                        "unsupported_claims_forbidden": True,
                        "complete_all_pending_fields": True,
                        "required_fields_must_be_completed_first": True,
                        "manual_entry_only_after_intelligence_unresolved": True,
                        "objective_values_require_evidence_refs": True,
                        "unverifiable_fields_must_be_unresolved": True,
                        "visual_supported_fields_must_inspect_supplier_images": True,
                        "generated_images_are_product_fact_evidence": False,
                        "unresolved_requires_resolution_class": True,
                        "customer_facing_text_must_be_russian": True,
                        "translation_and_exact_unit_normalization_allowed": True,
                    },
                    "completed_fields": generated.get("fields") or {},
                    "completed_field_results": generated_field_results,
                    "pending_field_count": pending_field_count,
                    "unresolved_field_count": unresolved_field_count,
                    "blocking_unresolved_field_count": (
                        blocking_unresolved_field_count
                    ),
                    "existing_mapped_field_count": item.get(
                        "mapped_attribute_count", 0
                    ),
                    "template_field_count": item.get("attribute_schema_count", 0),
                }
            )

        pending = sum(1 for item in task_items if item["status"] == "pending")
        ready = sum(1 for item in task_items if item["status"] == "ready")
        completed_with_gaps = sum(
            1 for item in task_items if item["status"] == "completed_with_gaps"
        )
        blocked = sum(1 for item in task_items if item["status"] == "blocked")
        completed = ready + completed_with_gaps + blocked
        pending_fields = sum(item["pending_field_count"] for item in task_items)
        unresolved_fields = sum(item["unresolved_field_count"] for item in task_items)
        completed_fields = sum(
            len(item["field_tasks"]) - item["pending_field_count"]
            for item in task_items
        )
        return Result.success(
            "content_tasks.loaded",
            "Intelligent field-draft tasks loaded from Ozon and supplier evidence.",
            {
                "run_id": run_id,
                "summary": {
                    "total": len(task_items),
                    "pending": pending,
                    "completed": completed,
                    "ready": ready,
                    "completed_with_gaps": completed_with_gaps,
                    "blocked": blocked,
                    "pending_fields": pending_fields,
                    "completed_fields": completed_fields,
                    "unresolved_fields": unresolved_fields,
                    "target_score": 90,
                },
                "items": task_items,
                "boundaries": {
                    "code_changes": False,
                    "image_generation": False,
                    "upload": False,
                    "publish_approval": False,
                },
            },
        )

    def complete_content_task(self, run_id: str, payload: dict[str, Any]) -> Result:
        tasks = self.content_tasks(run_id)
        if not tasks.ok:
            return tasks
        seed_id = str(payload.get("seed_id") or "").strip()
        task = next(
            (
                item
                for item in tasks.data.get("items", [])
                if str(item.get("seed_id") or "") == seed_id
            ),
            None,
        )
        if task is None:
            return Result.failure(
                "content_task.not_found",
                "No Ozon content task exists for this product.",
                data={"run_id": run_id, "seed_id": seed_id},
            )
        raw_fields = payload.get("fields")
        fields = (
            {str(key): value for key, value in raw_fields.items()}
            if isinstance(raw_fields, dict)
            else {}
        )
        task_fields = {
            str(field.get("field_key") or ""): field
            for field in task.get("field_tasks", [])
            if str(field.get("field_key") or "")
        }
        expected = {
            key: field
            for key, field in task_fields.items()
            if field.get("status") == "pending"
        }
        missing = sorted(key for key in expected if key not in fields)
        unexpected = sorted(key for key in fields if key not in expected)
        errors: list[str] = []
        if missing:
            errors.append(f"Missing intelligent field decisions: {', '.join(missing)}")
        if unexpected:
            errors.append(f"Unexpected template fields: {', '.join(unexpected)}")

        evidence = task.get("evidence") or {}
        evidence_index = task.get("evidence_index") or {}
        normalized_results: dict[str, dict[str, Any]] = {}
        for field_key, field in expected.items():
            raw_value = fields.get(field_key)
            mode = str(field.get("mode") or "")
            if isinstance(raw_value, dict):
                decision = str(raw_value.get("decision") or "").strip()
                value = raw_value.get("value")
                evidence_refs = raw_value.get("evidence_refs")
                reason = str(raw_value.get("reason") or "").strip()
                raw_visual_analysis = raw_value.get("visual_analysis")
                resolution_class = str(
                    raw_value.get("resolution_class") or ""
                ).strip()
            elif mode == "creative_rewrite" and _has_content_value(raw_value):
                decision = "filled"
                value = raw_value
                evidence_refs = []
                reason = "Original Russian content generated from collected evidence."
                raw_visual_analysis = None
                resolution_class = ""
            else:
                decision = ""
                value = None
                evidence_refs = []
                reason = ""
                raw_visual_analysis = None
                resolution_class = ""
            evidence_refs = (
                [
                    str(reference)
                    for reference in evidence_refs
                    if str(reference or "").strip()
                ]
                if isinstance(evidence_refs, list)
                else []
            )
            label = str(field.get("label") or field_key)
            if decision not in {"filled", "unresolved"}:
                errors.append(
                    f"{label}: decision must be filled or unresolved."
                )
                continue
            unknown_refs = [
                reference
                for reference in evidence_refs
                if reference not in evidence_index
            ]
            if unknown_refs:
                errors.append(
                    f"{label}: unknown evidence_refs: {', '.join(unknown_refs)}"
                )
            visual_evidence_refs = set(field.get("visual_evidence_refs") or [])
            if visual_evidence_refs and not visual_evidence_refs.intersection(
                evidence_refs
            ):
                errors.append(
                    f"{label}: inspect and cite a locked 1688 visual evidence_ref."
                )
            visual_analysis, visual_errors = _validated_visual_analysis(
                field,
                raw_visual_analysis,
                sorted(visual_evidence_refs),
                decision,
                value=value,
                resolution_class=resolution_class,
            )
            errors.extend(visual_errors)
            if decision == "unresolved":
                if mode == "creative_rewrite":
                    errors.append(f"{label}: creative content must be completed.")
                if resolution_class not in _CONTENT_RESOLUTION_CLASSES:
                    errors.append(
                        f"{label}: unresolved decisions require resolution_class "
                        f"from {', '.join(sorted(_CONTENT_RESOLUTION_CLASSES))}."
                    )
                if len(reason) < 10:
                    errors.append(
                        f"{label}: unresolved decisions require a concrete reason."
                    )
                if _has_content_value(value):
                    errors.append(
                        f"{label}: unresolved decisions must not contain a value."
                    )
            else:
                if not _has_content_value(value):
                    errors.append(f"{label}: a filled decision requires a value.")
                if mode == "evidence_inference":
                    if not evidence_refs:
                        errors.append(
                            f"{label}: evidence_refs are required for objective fields."
                        )
                    field_specific_refs = {
                        str(reference)
                        for reference in (
                            field.get("candidate_evidence_refs") or []
                        )
                        if str(reference or "").strip()
                    }
                    field_specific_refs.update(visual_evidence_refs)
                    if evidence_refs and not field_specific_refs.intersection(
                        evidence_refs
                    ):
                        errors.append(
                            f"{label}: a filled objective field must cite a "
                            "field-specific evidence_ref."
                        )
                    allowed_values = field.get("allowed_values") or []
                    if (
                        visual_evidence_refs
                        and allowed_values
                        and str(value or "").casefold()
                        not in {
                            str(allowed).casefold()
                            for allowed in allowed_values
                        }
                    ):
                        errors.append(
                            f"{label}: visual value must match an allowed Seller "
                            "API dictionary value."
                        )
                    if field.get("supplier_truth_required") is True and not any(
                        reference.startswith(
                            ("supplier.", "supplier_selection.")
                        )
                        for reference in evidence_refs
                    ):
                        errors.append(
                            f"{label}: supplier identity requires a 1688 evidence_ref."
                        )
                    if (
                        _field_requires_russian_objective_text(field)
                        and re.search(r"[\u3400-\u9fff]", str(value or ""))
                    ):
                        errors.append(
                            f"{label}: Russian text is required for customer-facing "
                            "objective fields; translate the verified supplier fact "
                            "without changing its meaning."
                        )
            normalized_results[field_key] = {
                "decision": decision,
                "value": value,
                "evidence_refs": evidence_refs,
                "reason": reason,
                "mode": mode,
                "label": label,
                "resolution_class": (
                    resolution_class if decision == "unresolved" else None
                ),
                "visual_analysis": visual_analysis,
            }

        visual_signatures: dict[tuple[str, str], tuple[str, str]] = {}
        for field_key, field_result in normalized_results.items():
            visual_analysis = field_result.get("visual_analysis")
            if not isinstance(visual_analysis, dict):
                continue
            field = expected.get(field_key) or {}
            canonical_label = canonical_attribute_label(field.get("label"))
            signature = (
                _normalize_content_text(field_result.get("reason")),
                _normalize_content_text(visual_analysis.get("field_finding")),
            )
            previous = visual_signatures.get(signature)
            if previous and previous[0] != canonical_label:
                errors.append(
                    f"{field_result.get('label')}: visual reason and field_finding "
                    f"duplicate the unrelated field {previous[1]}."
                )
            else:
                visual_signatures[signature] = (
                    canonical_label,
                    str(field_result.get("label") or field_key),
                )

        source_title = str(evidence.get("ozon_title_style_reference") or "")
        description_blocks = (
            (evidence.get("ozon_content_score_evidence") or {}).get(
                "description_or_rich_content_blocks"
            )
            or []
        )
        normalized_source_title = _normalize_content_text(source_title)
        normalized_source_blocks = {
            _normalize_content_text(value)
            for value in description_blocks
            if _has_content_value(value)
        }
        for field_key, field in expected.items():
            if str(field.get("mode") or "") != "creative_rewrite":
                continue
            field_result = normalized_results.get(field_key) or {}
            if field_result.get("decision") != "filled":
                continue
            label = str(field.get("label") or field_key)
            value = field_result.get("value")
            if not _has_content_value(value):
                continue
            text_value = str(value).strip()
            canonical_label = canonical_attribute_label(field.get("label"))
            normalized_label = _normalize_content_text(label)
            normalized_value = _normalize_content_text(text_value)
            if not re.search(r"[А-Яа-яЁё]", text_value):
                errors.append(f"{label}: Russian text is required.")
            if normalized_label == "название":
                if not 20 <= len(text_value) <= 200:
                    errors.append(f"{label}: length must be 20-200 characters.")
                if normalized_source_title and normalized_value == normalized_source_title:
                    errors.append(f"{label}: must be recreated, not copied from Ozon.")
            elif normalized_label in {"аннотация", "описание"}:
                if not 120 <= len(text_value) <= 5000:
                    errors.append(f"{label}: length must be 120-5000 characters.")
                if normalized_value in normalized_source_blocks:
                    errors.append(f"{label}: must be recreated, not copied from Ozon.")
            elif "rich" in normalized_label:
                try:
                    rich_content = json.loads(text_value)
                except (TypeError, ValueError, json.JSONDecodeError):
                    errors.append(f"{label}: valid JSON is required.")
                else:
                    if not isinstance(rich_content, (dict, list)) or not rich_content:
                        errors.append(f"{label}: JSON content must not be empty.")
            elif canonical_label == "hashtags":
                if not _valid_ozon_hashtags(text_value, minimum_count=3):
                    errors.append(
                        f"{label}: at least three Russian hashtags are required; "
                        "every token must start with # and contain no spaces."
                    )

        if errors:
            return Result.failure(
                "content_task.validation_failed",
                "Intelligent field draft did not pass evidence and originality validation.",
                errors=errors,
                data={"run_id": run_id, "seed_id": seed_id},
            )

        result_path = self.repo.run_dir(run_id) / "generated_content_result.json"
        if result_path.exists():
            result_payload = self.repo.load_generated_content_result(run_id)
        else:
            result_payload = {"schema_version": 1, "run_id": run_id, "items": {}}
        result_items = result_payload.get("items")
        if not isinstance(result_items, dict):
            result_items = {}
            result_payload["items"] = result_items
        existing_item = (
            result_items.get(seed_id)
            if isinstance(result_items.get(seed_id), dict)
            else {}
        )
        stored_field_results = {
            field_key: result
            for field_key, result in _generated_field_results(existing_item).items()
            if field_key in task_fields
        }
        stored_field_results.update(normalized_results)
        flattened_fields = {
            key: result.get("value")
            for key, result in stored_field_results.items()
            if result.get("decision") == "filled"
            and _has_content_value(result.get("value"))
        }
        completed_at = utc_now_iso()
        result_items[seed_id] = {
            "status": "completed",
            "target_score": 90,
            "fields": flattened_fields,
            "field_results": stored_field_results,
            "completed_at": completed_at,
            "evidence_policy": "ozon_structure_reference_plus_verified_product_facts",
        }
        result_payload["schema_version"] = 2
        result_payload["updated_at"] = completed_at
        unresolved_field_count = sum(
            1
            for result in stored_field_results.values()
            if result.get("decision") == "unresolved"
        )
        required_field_keys = {
            str(field.get("field_key") or "")
            for field in task.get("field_tasks", [])
            if field.get("required") is True
        }
        blocking_unresolved_field_count = sum(
            1
            for field_key, result in stored_field_results.items()
            if field_key in required_field_keys
            and result.get("decision") == "unresolved"
        )
        if blocking_unresolved_field_count:
            completion_status = "blocked"
        elif unresolved_field_count:
            completion_status = "completed_with_gaps"
        else:
            completion_status = "completed"
        result_items[seed_id]["status"] = completion_status
        path = self.repo.save_generated_content_result(run_id, result_payload)
        self.repo.append_run_event(
            run_id,
            "content_task.completed",
            "Evidence-constrained intelligent field draft was validated and saved without publishing.",
            {
                "seed_id": seed_id,
                "field_keys": sorted(expected),
                "unresolved_field_count": unresolved_field_count,
                "blocking_unresolved_field_count": (
                    blocking_unresolved_field_count
                ),
                "status": completion_status,
                "target_score": 90,
                "result_path": str(path),
            },
        )
        return Result.success(
            "content_task.completed",
            "Intelligent field draft was validated and saved.",
            {
                "run_id": run_id,
                "seed_id": seed_id,
                "status": completion_status,
                "field_count": len(expected),
                "unresolved_field_count": unresolved_field_count,
                "blocking_unresolved_field_count": (
                    blocking_unresolved_field_count
                ),
                "target_score": 90,
            },
        )

    def build_upload_draft(
        self,
        run_id: str,
        *,
        seed_ids: list[str] | None = None,
    ) -> Result:
        workspace = self.upload_workspace(run_id)
        if not workspace.ok:
            return workspace
        requested_seed_ids = {
            str(seed_id).strip()
            for seed_id in (seed_ids or [])
            if str(seed_id).strip()
        }
        ready_items = [
            item
            for item in workspace.data.get("items", [])
            if item.get("ready_to_build") is True
            and (
                not requested_seed_ids
                or str(item.get("seed_id") or "") in requested_seed_ids
            )
        ]
        if not ready_items:
            selected_items = [
                item
                for item in workspace.data.get("items", [])
                if not requested_seed_ids
                or str(item.get("seed_id") or "") in requested_seed_ids
            ]
            return Result.failure(
                "upload_draft.no_ready_products",
                "No product currently passes its own template, required-attribute, and image gates.",
                data={
                    "run_id": run_id,
                    "prepared_product_count": 0,
                    "items": [],
                    "blocked_items": [
                        {
                            "seed_id": item.get("seed_id"),
                            "blocking_gates": item.get("blocking_gates") or [],
                            "missing_required_fields": item.get(
                                "missing_required_fields"
                            )
                            or [],
                            "category_template_assessment": item.get(
                                "category_template_assessment"
                            )
                            or {},
                        }
                        for item in selected_items
                    ],
                },
            )
        draft_items = []
        blocked_items = []
        for item in ready_items:
            draft_attributes = []
            unresolved_required_dictionaries = []
            omitted_optional_dictionary_fields = []
            for field in item.get("attribute_mapping", []):
                if field.get("status") != "mapped":
                    continue
                upload_value = _dictionary_upload_value(item, field)
                if canonical_attribute_label(field.get("label")) == "rich_content":
                    upload_value = _normalize_ozon_rich_content_value(upload_value)
                draft_attribute = {
                    "attribute_id": field["field_key"],
                    "label": field["label"],
                    "value": upload_value,
                    "attribute_type": field.get("attribute_type"),
                    "dictionary_id": field.get("dictionary_id"),
                    "dictionary_value_id": None,
                    "dictionary_resolution_required": field.get(
                        "dictionary_resolution_required", False
                    ),
                    "evidence_ref": field.get("evidence_ref"),
                }
                if field.get("dictionary_resolution_required"):
                    try:
                        resolved = self.seller_api_adapter.resolve_attribute_dictionary_value(
                            description_category_id=int(item["description_category_id"]),
                            type_id=int(item["type_id"]),
                            attribute_id=int(field["field_key"]),
                            value=str(upload_value),
                        )
                    except (SellerApiError, TypeError, ValueError) as exc:
                        draft_attribute["dictionary_resolution_error"] = str(exc)
                        if field.get("required") is True:
                            unresolved_required_dictionaries.append(
                                {
                                    "field_key": field["field_key"],
                                    "label": field["label"],
                                    "value": upload_value,
                                    "error": str(exc),
                                }
                            )
                        else:
                            omitted_optional_dictionary_fields.append(
                                {
                                    "field_key": field["field_key"],
                                    "label": field["label"],
                                    "value": upload_value,
                                    "error": str(exc),
                                }
                            )
                            continue
                    else:
                        draft_attribute["dictionary_value_id"] = resolved[
                            "dictionary_value_id"
                        ]
                        draft_attribute["dictionary_value"] = resolved["value"]
                        draft_attribute["value"] = resolved["value"]
                draft_attributes.append(draft_attribute)
            if unresolved_required_dictionaries:
                blocked_items.append(
                    {
                        "seed_id": item["seed_id"],
                        "reason": "required_dictionary_value_unresolved",
                        "fields": unresolved_required_dictionaries,
                    }
                )
                continue
            draft_items.append(
                {
                    "seed_id": item["seed_id"],
                    "status": "attribute_prefill_ready",
                    "description_category_id": item.get("description_category_id"),
                    "type_id": item.get("type_id"),
                    "category_path": item.get("category_path"),
                    "source_title": item.get("source_title"),
                    "supplier_offer_id": item.get("supplier_offer_id"),
                    "upload_core_fields": item.get("upload_core_fields"),
                    "pricing_evidence": item.get("pricing_evidence"),
                    "attributes": draft_attributes,
                    "video_template_fields": item.get("video_template_fields") or [],
                    "omitted_optional_dictionary_fields": (
                        omitted_optional_dictionary_fields
                    ),
                    "content_optimization": {
                        "target_score": 90,
                        "status": (
                            "rewrite_required"
                            if any(
                                field.get("status") == "rewrite_required"
                                for field in item.get("attribute_mapping", [])
                            )
                            else "not_required"
                        ),
                        "rewrite_fields": [
                            {
                                "field_key": field.get("field_key"),
                                "label": field.get("label"),
                                "reference_evidence": field.get("reference_evidence", []),
                            }
                            for field in item.get("attribute_mapping", [])
                            if field.get("status") == "rewrite_required"
                        ],
                        "objective_evidence": {
                            "ozon_title_reference_only": item.get("source_title"),
                            "ozon_attributes": item.get("source_attributes") or {},
                            "ozon_content_score_evidence": item.get("source_content_score_evidence") or {},
                            "supplier_attributes": item.get("supplier_attributes") or {},
                            "confirmed_supplier_sku": item.get("supplier_selected_sku") or {},
                            "ozon_selected_sku": item.get("selected_options") or {},
                        },
                        "policy": (
                            "Generate original Russian content from verified facts. "
                            "Do not copy Ozon title, description, or rich content."
                        ),
                    },
                    "bootstrap_image_url": item.get("bootstrap_image_url"),
                    "subject_master": item.get("subject_master") or {},
                    "locked_supplier_sku": item.get(
                        "supplier_selected_sku"
                    )
                    or {},
                    "ozon_reference_images": item.get(
                        "ozon_reference_images"
                    )
                    or [],
                }
            )
        payload = {
            "schema_version": 1,
            "run_id": run_id,
            "kind": "per_product_attribute_prefill_draft",
            "prepared_at": utc_now_iso(),
            "prepared_product_count": len(draft_items),
            "blocked_product_count": len(blocked_items),
            "items": draft_items,
            "blocked_items": blocked_items,
            "publish_locked": True,
        }
        path = self.repo.save_upload_draft(run_id, payload)
        event = self.repo.append_run_event(
            run_id,
            "upload_draft.prepared",
            "Ready products were written to an isolated attribute prefill draft without publishing.",
            {
                "prepared_product_count": len(draft_items),
                "seed_ids": [item["seed_id"] for item in draft_items],
                "blocked_seed_ids": [item["seed_id"] for item in blocked_items],
                "draft_path": str(path),
            },
        )
        if not draft_items:
            return Result.failure(
                "upload_draft.required_dictionary_values_unresolved",
                "Required Seller API dictionary values could not be resolved exactly.",
                data={**payload, "draft_path": str(path), "last_event": event.to_dict()},
            )
        return Result.success(
            "upload_draft.prepared",
            "Ready product attribute drafts were prepared. Nothing was submitted to Ozon.",
            {**payload, "draft_path": str(path), "last_event": event.to_dict()},
        )

    def preview_product_upload(self, run_id: str, seed_id: str) -> Result:
        draft = self.build_upload_draft(run_id, seed_ids=[seed_id])
        draft_item = next(
            (
                item
                for item in draft.data.get("items", [])
                if str(item.get("seed_id") or "") == seed_id
            ),
            None,
        )
        if draft_item is None:
            blocked_item = next(
                (
                    item
                    for item in draft.data.get("blocked_items", [])
                    if str(item.get("seed_id") or "") == seed_id
                ),
                None,
            )
            blocking_gates = (
                blocked_item.get("blocking_gates") or []
                if isinstance(blocked_item, dict)
                else []
            )
            if "category_template" in blocking_gates:
                refreshed = self.refresh_attribute_template(run_id, seed_id)
                if refreshed.ok:
                    draft = self.build_upload_draft(
                        run_id,
                        seed_ids=[seed_id],
                    )
                    draft_item = next(
                        (
                            item
                            for item in draft.data.get("items", [])
                            if str(item.get("seed_id") or "") == seed_id
                        ),
                        None,
                    )
                    blocked_item = next(
                        (
                            item
                            for item in draft.data.get("blocked_items", [])
                            if str(item.get("seed_id") or "") == seed_id
                        ),
                        None,
                    )
            if draft_item is not None:
                blocked_item = None
            else:
                unresolved_dictionary = (
                    draft.code
                    == "upload_draft.required_dictionary_values_unresolved"
                )
                unresolved_fields = (
                    blocked_item.get("fields", [])
                    if isinstance(blocked_item, dict)
                    else []
                )
                unresolved_details = ", ".join(
                    f"{str(field.get('label') or field.get('field_key') or 'unknown field')}"
                    f"={str(field.get('value') or '').strip() or '<empty>'}"
                    for field in unresolved_fields
                    if isinstance(field, dict)
                )
                return Result.failure(
                    (
                        "product_upload.required_dictionary_values_unresolved"
                        if unresolved_dictionary
                        else "product_upload.product_not_ready"
                    ),
                    (
                        "Required Ozon dictionary fields could not be resolved "
                        f"exactly: {unresolved_details}."
                        if unresolved_dictionary
                        else "This product does not yet pass its own upload gates."
                    ),
                    data={
                        "run_id": run_id,
                        "seed_id": seed_id,
                        "draft_code": draft.code,
                        "blocked_item": blocked_item,
                    },
                )
        bootstrap_image_url = str(
            draft_item.get("bootstrap_image_url") or ""
        ).strip()
        if not _is_public_https_url(bootstrap_image_url):
            return Result.failure(
                "product_upload.bootstrap_image_required",
                "Lock one public 1688 original subject image before preparing the Ozon product.",
                data={"run_id": run_id, "seed_id": seed_id},
            )
        image_urls = [bootstrap_image_url]
        try:
            currency_code = self.seller_api_adapter.get_seller_currency_code()
        except SellerApiError as exc:
            return Result.failure(
                "product_upload.seller_currency_failed",
                str(exc),
                data={"run_id": run_id, "seed_id": seed_id},
            )
        upload_core_fields = _pricing_upload_core_fields(
            draft_item.get("pricing_evidence") or {},
            currency_code=currency_code,
        )
        if upload_core_fields is None:
            return Result.failure(
                "product_upload.pricing_currency_unsupported",
                "Confirmed pricing evidence cannot be converted to the store contract currency.",
                data={
                    "run_id": run_id,
                    "seed_id": seed_id,
                    "currency_code": currency_code,
                },
            )
        draft_item = {**draft_item, "upload_core_fields": upload_core_fields}
        seller_api_item = _seller_api_import_item(draft_item, image_urls)
        canonical = json.dumps(
            seller_api_item,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        confirmation_token = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        preview = {
            "schema_version": 1,
            "run_id": run_id,
            "seed_id": seed_id,
            "prepared_at": utc_now_iso(),
            "confirmation_token": confirmation_token,
            "seller_api_item": seller_api_item,
            "bootstrap_image": {
                "source": "locked_1688_subject_original",
                "url": bootstrap_image_url,
            },
            "image_task_context": {
                "subject_master": draft_item.get("subject_master") or {},
                "locked_supplier_sku": draft_item.get(
                    "locked_supplier_sku"
                )
                or {},
                "ozon_reference_images": draft_item.get(
                    "ozon_reference_images"
                )
                or [],
                "video_template_fields": draft_item.get(
                    "video_template_fields"
                )
                or [],
            },
            "status": "awaiting_user_confirmation",
        }
        with self._upload_state_lock:
            previews = {"schema_version": 1, "run_id": run_id, "items": {}}
            try:
                saved = self.repo.load_upload_previews(run_id)
            except FileNotFoundError:
                saved = None
            if isinstance(saved, dict):
                previews.update(saved)
            if not isinstance(previews.get("items"), dict):
                previews["items"] = {}
            previews["items"][seed_id] = preview
            path = self.repo.save_upload_previews(run_id, previews)
            event = self.repo.append_run_event(
                run_id,
                "product_upload.preview_ready",
                "A final Seller API payload was prepared for one product and is awaiting user confirmation.",
                {
                    "seed_id": seed_id,
                    "offer_id": seller_api_item["offer_id"],
                    "image_count": len(image_urls),
                    "preview_path": str(path),
                },
            )
        return Result.success(
            "product_upload.preview_ready",
            "Final product upload payload is ready for explicit confirmation.",
            {**preview, "preview_path": str(path), "last_event": event.to_dict()},
        )

    def _emit_image_task_package(
        self,
        *,
        run_id: str,
        seed_id: str,
        seller_import_task_id: int,
        ozon_product_id: int | None,
        preview: dict[str, Any],
    ) -> dict[str, str]:
        package_id = "ozon-image-" + hashlib.sha256(
            f"{run_id}:{seed_id}".encode("utf-8")
        ).hexdigest()[:24]
        seller_api_item = preview.get("seller_api_item") or {}
        image_task_context = preview.get("image_task_context") or {}
        subject_master = image_task_context.get("subject_master") or {}
        bootstrap_image = preview.get("bootstrap_image") or {}
        package_created_at = utc_now_iso()
        try:
            batch_created_at = str(
                self.repo.load_run(run_id).get("created_at") or package_created_at
            )
        except FileNotFoundError:
            batch_created_at = package_created_at
        package = {
            "schema_version": 3,
            "kind": "ozon_product_image_generation_and_upload",
            "package_id": package_id,
            "status": "pending",
            "created_at": package_created_at,
            "batch_created_at": batch_created_at,
            "run_id": run_id,
            "seed_id": seed_id,
            "store_target": {
                "credential_ref": "configured_store",
                "seller_import_task_id": seller_import_task_id,
                "offer_id": seller_api_item.get("offer_id"),
                "product_id": ozon_product_id,
                "binding_state": (
                    "bound" if ozon_product_id else "awaiting_ozon_product"
                ),
                "seller_api_item": seller_api_item,
                "video_template_fields": image_task_context.get(
                    "video_template_fields"
                )
                or [],
            },
            "bootstrap_image": {
                "source": "locked_1688_subject_original",
                "url": bootstrap_image.get("url"),
                "subject_master_sha256": subject_master.get(
                    "subject_master_sha256"
                ),
            },
            "generation_contract": {
                "slot_count": 8,
                "aspect_ratio": "3:4",
                "generation_mode": "single_thread_8_grid",
                "grid_layout": "4x2",
                "public_media": "auto_quick_tunnel",
                "direct_ozon_upload": True,
                "replace_complete_gallery": True,
                "return_to_workbench": False,
                "identity_reference": {
                    "required": True,
                    "source": "generated_white_anchor",
                    "reference_index": 1,
                    "reference_count": 1,
                    "additional_image_references_allowed": False,
                    "reuse_for_all_finished_calls": True,
                    "reuse_for_repairs": True,
                    "product_identity_source": "white_anchor_only",
                    "composition_source": "fixed_skill_prompt_only",
                },
                "video": {
                    "required": True,
                    "source": "eight_accepted_images",
                    "format": "mp4",
                    "layout": "3:4_vertical_slideshow",
                },
                "video_cover": {
                    "required": True,
                    "source": "main_01",
                    "format": "jpg",
                    "aspect_ratio": "3:4",
                },
            },
            "evidence": {
                "subject_master": subject_master,
                "locked_supplier_sku": image_task_context.get(
                    "locked_supplier_sku"
                )
                or {},
            },
        }
        path = self.repo.save_image_task_package(package_id, package)
        return {
            "image_task_package_id": package_id,
            "image_task_package_path": str(path),
            "image_task_package_status": "pending",
        }

    def _sync_image_task_record(
        self,
        *,
        run_id: str,
        seed_id: str,
        record: dict[str, Any],
        preview: dict[str, Any] | None,
        seller_status: dict[str, Any] | None = None,
    ) -> None:
        try:
            seller_import_task_id = int(record.get("task_id") or 0)
        except (TypeError, ValueError):
            seller_import_task_id = 0
        if seller_import_task_id <= 0:
            return
        effective_status = seller_status or record.get("seller_api_status") or {}
        ozon_product_id: int | None = None
        if str(record.get("status") or "") == "accepted_by_ozon":
            ozon_product_id = _product_id_from_import_status(
                effective_status,
                offer_id=str(record.get("offer_id") or ""),
            )
            if ozon_product_id is None:
                product_state = record.get("seller_product_state")
                if isinstance(product_state, dict):
                    ozon_product_id = _product_id_from_import_status(
                        {"items": [product_state]},
                        offer_id=str(record.get("offer_id") or ""),
                    )
        package_id = str(record.get("image_task_package_id") or "").strip()
        if not package_id:
            if not isinstance(preview, dict):
                record["image_task_package_error"] = (
                    "The submitted product has no saved upload preview; "
                    "regenerate the preview before creating its image task."
                )
                return
            record.update(
                self._emit_image_task_package(
                    run_id=run_id,
                    seed_id=seed_id,
                    seller_import_task_id=seller_import_task_id,
                    ozon_product_id=ozon_product_id,
                    preview=preview,
                )
            )
            package_id = str(record["image_task_package_id"])
        located = self.repo.find_image_task_package(package_id)
        if located is None:
            if not isinstance(preview, dict):
                record["image_task_package_error"] = (
                    "The submitted product has no saved upload preview; "
                    "regenerate the preview before recreating its image task."
                )
                return
            record.update(
                self._emit_image_task_package(
                    run_id=run_id,
                    seed_id=seed_id,
                    seller_import_task_id=seller_import_task_id,
                    ozon_product_id=ozon_product_id,
                    preview=preview,
                )
            )
            package_id = str(record["image_task_package_id"])
        if ozon_product_id is None:
            located = self.repo.find_image_task_package(package_id)
            if located is None:
                record["image_task_package_error"] = (
                    "The deferred image task package could not be located."
                )
                return
            bound_path, _payload = located
            record["image_task_package_path"] = str(bound_path)
            record["image_task_package_status"] = bound_path.parent.name
            record.pop("image_task_package_error", None)
            return
        try:
            bound_path = self.repo.bind_pending_image_task_product(
                package_id,
                run_id=run_id,
                seed_id=seed_id,
                seller_import_task_id=seller_import_task_id,
                product_id=ozon_product_id,
            )
        except (FileNotFoundError, TypeError, ValueError) as exc:
            record["image_task_package_error"] = str(exc)
            return
        record["image_task_package_path"] = str(bound_path)
        record["image_task_package_status"] = bound_path.parent.name
        record.pop("image_task_package_error", None)

    def submit_product_upload(
        self,
        run_id: str,
        seed_id: str,
        *,
        confirmation_token: str,
    ) -> Result:
        try:
            previews = self.repo.load_upload_previews(run_id)
        except FileNotFoundError:
            return Result.failure(
                "product_upload.preview_required",
                "Prepare and review the final product payload before uploading.",
                data={"run_id": run_id, "seed_id": seed_id},
            )
        preview = (previews.get("items") or {}).get(seed_id)
        if not isinstance(preview, dict):
            return Result.failure(
                "product_upload.preview_required",
                "Prepare and review the final product payload before uploading.",
                data={"run_id": run_id, "seed_id": seed_id},
            )
        expected_token = str(preview.get("confirmation_token") or "")
        if not confirmation_token or confirmation_token != expected_token:
            return Result.failure(
                "product_upload.confirmation_mismatch",
                "The upload preview changed or was not explicitly confirmed.",
                data={"run_id": run_id, "seed_id": seed_id},
            )
        submissions = {"schema_version": 1, "run_id": run_id, "items": {}}
        try:
            saved = self.repo.load_upload_submissions(run_id)
        except FileNotFoundError:
            saved = None
        if isinstance(saved, dict):
            submissions.update(saved)
        if not isinstance(submissions.get("items"), dict):
            submissions["items"] = {}
        existing = submissions["items"].get(seed_id)
        existing_status = (
            str(existing.get("status") or "").casefold()
            if isinstance(existing, dict)
            else ""
        )
        if (
            isinstance(existing, dict)
            and existing.get("task_id") is not None
            and existing_status not in {"failed", "error", "declined"}
        ):
            return Result.success(
                "product_upload.already_submitted",
                "This product upload is already being processed or was accepted.",
                existing,
            )
        current_preview = self.preview_product_upload(run_id, seed_id)
        if not current_preview.ok:
            return current_preview
        current_token = str(
            current_preview.data.get("confirmation_token") or ""
        )
        if current_token != expected_token:
            return Result.failure(
                "product_upload.confirmation_mismatch",
                "The product evidence changed after the saved preview. Review and confirm the refreshed upload payload.",
                data={
                    "run_id": run_id,
                    "seed_id": seed_id,
                    "confirmation_token": current_token,
                },
            )
        preview = current_preview.data
        seller_api_item = preview.get("seller_api_item")
        if not isinstance(seller_api_item, dict):
            return Result.failure(
                "product_upload.preview_invalid",
                "The saved upload preview is invalid; prepare it again.",
                data={"run_id": run_id, "seed_id": seed_id},
            )
        try:
            submitted = self.seller_api_adapter.import_products([seller_api_item])
        except SellerApiError as exc:
            return Result.failure(
                "product_upload.seller_api_failed",
                str(exc),
                data={"run_id": run_id, "seed_id": seed_id},
            )
        record = {
            "run_id": run_id,
            "seed_id": seed_id,
            "offer_id": seller_api_item.get("offer_id"),
            "confirmation_token": expected_token,
            "task_id": submitted["task_id"],
            "status": "submitted",
            "submitted_at": utc_now_iso(),
        }
        if isinstance(existing, dict) and existing.get("task_id") is not None:
            history = [
                item
                for item in existing.get("attempt_history") or []
                if isinstance(item, dict)
            ]
            history.append(
                {
                    key: value
                    for key, value in existing.items()
                    if key != "attempt_history"
                }
            )
            record["attempt_history"] = history
            for key in (
                "image_task_package_id",
                "image_task_package_path",
                "image_task_package_status",
            ):
                if existing.get(key):
                    record[key] = existing[key]
        self._sync_image_task_record(
            run_id=run_id,
            seed_id=seed_id,
            record=record,
            preview=preview,
        )
        submissions["items"][seed_id] = record
        path = self.repo.save_upload_submissions(run_id, submissions)
        event = self.repo.append_run_event(
            run_id,
            "product_upload.submitted",
            "One explicitly confirmed product was submitted to Ozon Seller API.",
            {
                "seed_id": seed_id,
                "offer_id": record["offer_id"],
                "task_id": record["task_id"],
                "submission_path": str(path),
            },
        )
        return Result.success(
            "product_upload.submitted",
            "The confirmed product was submitted to Ozon.",
            {**record, "submission_path": str(path), "last_event": event.to_dict()},
        )

    def batch_upload_products(
        self,
        run_id: str,
        *,
        seed_ids: list[str] | None = None,
        confirmed: bool = False,
        max_workers: int = 4,
    ) -> Result:
        if confirmed is not True:
            return Result.failure(
                "batch_product_upload.confirmation_required",
                "Explicit confirmation is required before uploading multiple products.",
                data={"run_id": run_id},
            )
        requested = {
            str(seed_id).strip()
            for seed_id in (seed_ids or [])
            if str(seed_id).strip()
        }
        workspace = self.upload_workspace(run_id)
        if not workspace.ok:
            return workspace
        selected_items = [
            item
            for item in workspace.data.get("items", [])
            if not requested
            or str(item.get("seed_id") or "") in requested
        ]
        refresh_results: list[dict[str, Any]] = []
        for item in selected_items:
            if item.get("template_ready") is True:
                continue
            refreshed = self.refresh_attribute_template(
                run_id,
                str(item.get("seed_id") or ""),
            )
            refresh_results.append(
                {
                    "seed_id": item.get("seed_id"),
                    "ok": refreshed.ok,
                    "code": refreshed.code,
                    "message": refreshed.message,
                }
            )
        if refresh_results:
            workspace = self.upload_workspace(run_id)
            if not workspace.ok:
                return workspace
            selected_items = [
                item
                for item in workspace.data.get("items", [])
                if not requested
                or str(item.get("seed_id") or "") in requested
            ]
        selected_seed_ids = [
            str(item.get("seed_id") or "")
            for item in selected_items
            if str(item.get("seed_id") or "")
        ]
        if not selected_seed_ids:
            return Result.failure(
                "batch_product_upload.no_products",
                "No batch products were selected for upload.",
                data={"run_id": run_id, "items": []},
            )

        worker_count = max(
            1,
            min(int(max_workers or 1), 4, len(selected_seed_ids)),
        )
        preview_results: dict[str, Result] = {}
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(
                    self.preview_product_upload,
                    run_id,
                    seed_id,
                ): seed_id
                for seed_id in selected_seed_ids
            }
            for future in as_completed(futures):
                seed_id = futures[future]
                try:
                    preview_results[seed_id] = future.result()
                except Exception as exc:  # isolate one product from the batch
                    preview_results[seed_id] = Result.failure(
                        "batch_product_upload.preview_failed",
                        str(exc),
                        data={"run_id": run_id, "seed_id": seed_id},
                    )

        with self._upload_state_lock:
            submissions = {
                "schema_version": 1,
                "run_id": run_id,
                "items": {},
            }
            try:
                saved_submissions = self.repo.load_upload_submissions(run_id)
            except FileNotFoundError:
                saved_submissions = None
            if isinstance(saved_submissions, dict):
                submissions.update(saved_submissions)
            if not isinstance(submissions.get("items"), dict):
                submissions["items"] = {}

        result_items: dict[str, dict[str, Any]] = {}
        pending_imports: dict[str, dict[str, Any]] = {}
        for seed_id in selected_seed_ids:
            preview_result = preview_results[seed_id]
            if not preview_result.ok:
                result_items[seed_id] = {
                    "seed_id": seed_id,
                    "status": "blocked",
                    "code": preview_result.code,
                    "message": preview_result.message,
                    "errors": preview_result.errors,
                }
                continue
            existing = submissions["items"].get(seed_id)
            existing_status = (
                str(existing.get("status") or "").casefold()
                if isinstance(existing, dict)
                else ""
            )
            if (
                isinstance(existing, dict)
                and existing.get("task_id") is not None
                and existing_status not in {"failed", "error", "declined"}
            ):
                result_items[seed_id] = {
                    **existing,
                    "seed_id": seed_id,
                    "status": "already_submitted",
                }
                continue
            preview = preview_result.data
            seller_api_item = preview.get("seller_api_item")
            if not isinstance(seller_api_item, dict):
                result_items[seed_id] = {
                    "seed_id": seed_id,
                    "status": "failed",
                    "code": "product_upload.preview_invalid",
                    "message": "The saved upload preview is invalid.",
                }
                continue
            pending_imports[seed_id] = {
                "preview": preview,
                "seller_api_item": seller_api_item,
                "existing": existing,
            }

        def import_one(
            seed_id: str,
            pending: dict[str, Any],
        ) -> tuple[str, dict[str, Any]]:
            submitted = self.seller_api_adapter.import_products(
                [pending["seller_api_item"]]
            )
            return seed_id, submitted

        import_results: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(
            max_workers=max(
                1,
                min(worker_count, len(pending_imports) or 1),
            )
        ) as executor:
            futures = {
                executor.submit(import_one, seed_id, pending): seed_id
                for seed_id, pending in pending_imports.items()
            }
            for future in as_completed(futures):
                seed_id = futures[future]
                try:
                    _seed_id, submitted = future.result()
                except (SellerApiError, TypeError, ValueError) as exc:
                    result_items[seed_id] = {
                        "seed_id": seed_id,
                        "status": "failed",
                        "code": "product_upload.seller_api_failed",
                        "message": str(exc),
                    }
                else:
                    import_results[seed_id] = submitted

        for seed_id, submitted in import_results.items():
            pending = pending_imports[seed_id]
            seller_api_item = pending["seller_api_item"]
            preview = pending["preview"]
            record = {
                "run_id": run_id,
                "seed_id": seed_id,
                "offer_id": seller_api_item.get("offer_id"),
                "confirmation_token": preview.get("confirmation_token"),
                "task_id": submitted["task_id"],
                "status": "submitted",
                "submitted_at": utc_now_iso(),
            }
            existing = pending.get("existing")
            if isinstance(existing, dict) and existing.get("task_id") is not None:
                history = [
                    item
                    for item in existing.get("attempt_history") or []
                    if isinstance(item, dict)
                ]
                history.append(
                    {
                        key: value
                        for key, value in existing.items()
                        if key != "attempt_history"
                    }
                )
                record["attempt_history"] = history
                for key in (
                    "image_task_package_id",
                    "image_task_package_path",
                    "image_task_package_status",
                ):
                    if existing.get(key):
                        record[key] = existing[key]
            self._sync_image_task_record(
                run_id=run_id,
                seed_id=seed_id,
                record=record,
                preview=preview,
            )
            submissions["items"][seed_id] = record
            result_items[seed_id] = record

        with self._upload_state_lock:
            submission_path = self.repo.save_upload_submissions(
                run_id,
                submissions,
            )
        ordered_results = [
            result_items[seed_id]
            for seed_id in selected_seed_ids
            if seed_id in result_items
        ]
        submitted_count = sum(
            1
            for item in ordered_results
            if item.get("status") == "submitted"
        )
        already_submitted_count = sum(
            1
            for item in ordered_results
            if item.get("status") == "already_submitted"
        )
        event = self.repo.append_run_event(
            run_id,
            "batch_product_upload.completed",
            "Eligible products were validated and submitted independently with bounded concurrency.",
            {
                "requested_product_count": len(selected_seed_ids),
                "submitted_product_count": submitted_count,
                "already_submitted_product_count": already_submitted_count,
                "failed_or_blocked_product_count": (
                    len(ordered_results)
                    - submitted_count
                    - already_submitted_count
                ),
                "max_workers": worker_count,
            },
        )
        payload = {
            "run_id": run_id,
            "requested_product_count": len(selected_seed_ids),
            "submitted_product_count": submitted_count,
            "already_submitted_product_count": already_submitted_count,
            "failed_or_blocked_product_count": (
                len(ordered_results)
                - submitted_count
                - already_submitted_count
            ),
            "max_workers": worker_count,
            "template_refreshes": refresh_results,
            "items": ordered_results,
            "submission_path": str(submission_path),
            "last_event": event.to_dict(),
        }
        if submitted_count == 0 and already_submitted_count == 0:
            return Result.failure(
                "batch_product_upload.no_eligible_products",
                "No selected product passed validation and upload.",
                data=payload,
            )
        return Result.success(
            "batch_product_upload.completed",
            "Eligible products were validated and uploaded independently.",
            payload,
        )

    def refresh_product_upload_status(self, run_id: str, seed_id: str) -> Result:
        try:
            submissions = self.repo.load_upload_submissions(run_id)
        except FileNotFoundError:
            return Result.failure(
                "product_upload.not_submitted",
                "This product has not been submitted yet.",
                data={"run_id": run_id, "seed_id": seed_id},
            )
        record = (submissions.get("items") or {}).get(seed_id)
        if not isinstance(record, dict) or record.get("task_id") is None:
            return Result.failure(
                "product_upload.not_submitted",
                "This product has not been submitted yet.",
                data={"run_id": run_id, "seed_id": seed_id},
            )
        try:
            seller_status = self.seller_api_adapter.get_product_import_info(
                int(record["task_id"])
            )
        except (SellerApiError, TypeError, ValueError) as exc:
            return Result.failure(
                "product_upload.status_failed",
                str(exc),
                data={"run_id": run_id, "seed_id": seed_id},
            )
        record["seller_api_import_status"] = seller_status
        if _product_import_has_status(seller_status, "skipped"):
            offer_id = str(record.get("offer_id") or "").strip()
            if not offer_id:
                offer_id = _offer_id_from_import_status(seller_status)
            lookup = getattr(
                self.seller_api_adapter,
                "get_product_state_by_offer_id",
                None,
            )
            if offer_id and callable(lookup):
                try:
                    seller_product_state = lookup(offer_id)
                except SellerApiError as exc:
                    record["seller_product_state_error"] = str(exc)
                else:
                    if isinstance(seller_product_state, dict) and seller_product_state:
                        record["seller_product_state"] = seller_product_state
                        record.pop("seller_product_state_error", None)
                        seller_status = _reconcile_skipped_import_status(
                            seller_status,
                            seller_product_state,
                        )
        record["seller_api_status"] = seller_status
        record["status"] = _product_import_status(seller_status)
        record["status_checked_at"] = utc_now_iso()
        try:
            previews = self.repo.load_upload_previews(run_id)
        except FileNotFoundError:
            previews = {}
        preview = (previews.get("items") or {}).get(seed_id)
        self._sync_image_task_record(
            run_id=run_id,
            seed_id=seed_id,
            record=record,
            preview=preview if isinstance(preview, dict) else None,
            seller_status=seller_status,
        )
        submissions["items"][seed_id] = record
        self.repo.save_upload_submissions(run_id, submissions)
        return Result.success(
            "product_upload.status_loaded",
            "The latest Ozon import status was loaded.",
            record,
        )

    def batch_history(self) -> Result:
        items: list[dict[str, Any]] = []
        for run in self.repo.list_runs():
            if run.get("kind") != "workbench_batch":
                continue
            run_id = str(run.get("run_id") or "")
            if not run_id:
                continue
            status = str(run.get("status") or "unknown")
            items.append(
                {
                    "run_id": run_id,
                    "kind": run.get("kind"),
                    "status": status,
                    "target_count": int(run.get("target_count") or 0),
                    "created_at": run.get("created_at"),
                    "updated_at": run.get("updated_at") or run.get("created_at"),
                    "publish_locked": bool(run.get("publish_locked", True)),
                    "event_count": len(self.repo.load_run_events(run_id)),
                    "resume_url": f"/?run_id={run_id}",
                }
            )
        return Result.success(
            "operations.batches_loaded",
            "Batch history loaded.",
            {
                "items": items,
                "summary": {
                    "total": len(items),
                    "active": sum(1 for item in items if item["status"] != "done" and not item["status"].startswith("failed")),
                    "done": sum(1 for item in items if item["status"] == "done"),
                    "failed": sum(1 for item in items if item["status"].startswith("failed")),
                },
            },
        )

    def product_library(self) -> Result:
        products_by_id: dict[str, dict[str, Any]] = {}
        for run in self.repo.list_runs():
            run_id = str(run.get("run_id") or "")
            result_path = self.repo.run_dir(run_id) / "ozon_collection_result.json"
            if not result_path.exists():
                continue
            try:
                ozon_result = self.repo.load_ozon_collection_result(run_id)
            except (FileNotFoundError, TypeError):
                continue
            supplier_by_seed: dict[str, dict[str, Any]] = {}
            supplier_path = self.repo.run_dir(run_id) / "supplier_collection_result.json"
            if supplier_path.exists():
                supplier_result = self.repo.load_supplier_collection_result(run_id)
                supplier_by_seed = {
                    str(item.get("seed_id") or ""): item
                    for item in supplier_result.get("supplier_products", [])
                    if isinstance(item, dict)
                }
            for candidate in ozon_result.get("ozon_candidates", []):
                if not isinstance(candidate, dict):
                    continue
                product_id = str(candidate.get("ozon_product_id") or candidate.get("seed_id") or "")
                if not product_id or product_id in products_by_id:
                    continue
                media = candidate.get("selected_sku_media") or {}
                images = media.get("selected_sku_images") or media.get("main_gallery_images") or []
                supplier = supplier_by_seed.get(str(candidate.get("seed_id") or ""), {})
                products_by_id[product_id] = {
                    "product_id": product_id,
                    "run_id": run_id,
                    "title": candidate.get("title"),
                    "image": images[0] if images else None,
                    "category_path": candidate.get("category_path"),
                    "seller_name": candidate.get("seller_name"),
                    "price": candidate.get("price"),
                    "currency": candidate.get("currency"),
                    "rating": candidate.get("rating"),
                    "review_count": candidate.get("review_count"),
                    "ozon_url": candidate.get("ozon_url"),
                    "supplier_title": supplier.get("title"),
                    "supplier_url": supplier.get("supplier_url"),
                    "status": run.get("status"),
                }
        items = list(products_by_id.values())
        return Result.success(
            "operations.products_loaded",
            "Product library loaded.",
            {"items": items, "summary": {"total": len(items), "supplier_linked": sum(1 for item in items if item["supplier_url"])}},
        )

    def store_overview(self) -> Result:
        return Result.success(
            "operations.store_loaded",
            "Store authorization overview loaded.",
            {
                "credentials": self.repo.credential_status().to_dict(),
                "dedupe": self.repo.existing_store_dedupe_status(),
                "runtime_root": str(self.repo.context.runtime_root),
            },
        )

    def settings_overview(self) -> Result:
        context = self.repo.context
        return Result.success(
            "operations.settings_loaded",
            "Locked automation boundaries loaded.",
            {
                "config": {
                    "seed_pool_version": context.config.seed_pool_version,
                    "ozon_query_language": context.config.default_ozon_query_language,
                    "seed_source_language": context.config.default_seed_source_language,
                },
                "boundaries": {
                    "run_until_blocked": True,
                    "supplier_link_user_verified": True,
                    "single_sku": True,
                    "publish_locked_by_default": True,
                    "image_generation_enabled": False,
                },
                "routes": {
                    "ozon": "proxy_guest",
                    "1688": "direct_user_session",
                    "seller_api": "server_credentials",
                },
                "paths": {
                    "project_root": str(context.project_root),
                    "runtime_root": str(context.runtime_root),
                },
            },
        )

    def diagnostics_overview(self) -> Result:
        bridge = self.repo.load_browser_bridge_status()
        runs = self.repo.list_runs()
        recent_events: list[dict[str, Any]] = []
        for run in runs[:12]:
            run_id = str(run.get("run_id") or "")
            for event in self.repo.load_run_events(run_id)[-20:]:
                payload = event.to_dict()
                payload["run_id"] = run_id
                recent_events.append(payload)
        recent_events = sorted(recent_events, key=lambda item: str(item.get("created_at") or ""), reverse=True)[:120]
        return Result.success(
            "operations.diagnostics_loaded",
            "Diagnostics overview loaded.",
            {
                "browser_bridge": bridge,
                "runtime": {
                    "root": str(self.repo.context.runtime_root),
                    "run_count": len(runs),
                    "active_run_count": sum(1 for run in runs if run.get("status") not in {"done", "failed_blocked"}),
                },
                "recent_events": recent_events,
            },
        )

    def save_supplier_review_links(self, run_id: str, links: list[dict[str, Any]]) -> Result:
        run = self.repo.load_run(run_id)
        if WorkbenchState(run["status"]) != WorkbenchState.SUPPLIER_REVIEW:
            return Result.failure(
                "supplier_review.not_expected",
                "Supplier links can only be edited during supplier review.",
                data={"run_id": run_id, "status": run["status"]},
            )
        review = self.repo.load_supplier_review(run_id)
        items_by_seed = {str(item.get("seed_id")): item for item in review.get("items", [])}
        errors: list[str] = []
        for link in links:
            seed_id = str(link.get("seed_id") or "").strip()
            supplier_url = str(link.get("supplier_url") or "").strip()
            if seed_id not in items_by_seed:
                errors.append(f"Unknown seed_id: {seed_id or '(missing)'}")
                continue
            if not self._is_1688_product_url(supplier_url):
                errors.append(f"{seed_id}: a 1688 product detail URL is required.")
                continue
            items_by_seed[seed_id]["supplier_url"] = supplier_url
            items_by_seed[seed_id]["user_verified_exact_match"] = True
            items_by_seed[seed_id]["verified_at"] = utc_now_iso()
        if errors:
            return Result.failure(
                "supplier_review.invalid_link",
                "One or more supplier links are invalid.",
                errors=errors,
                data={"run_id": run_id, "status": run["status"]},
            )
        review["updated_at"] = utc_now_iso()
        self.repo.save_supplier_review(run_id, review)
        event = self.repo.append_run_event(
            run_id,
            "supplier_review.links_saved",
            "User-verified 1688 product links were saved.",
            {"saved_count": len(links), "ready_to_collect": self._supplier_review_complete(review)},
        )
        return Result.success(
            "supplier_review.links_saved",
            "User-verified 1688 product links were saved.",
            self._response_payload(
                run,
                event,
                {"supplier_review": review, "ready_to_collect": self._supplier_review_complete(review)},
            ),
        )

    def capture_supplier_selection_product(
        self,
        run_id: str,
        payload: dict[str, Any],
    ) -> Result:
        with self._run_mutation_lock(run_id):
            return self._capture_supplier_selection_product_locked(run_id, payload)

    def _capture_supplier_selection_product_locked(
        self,
        run_id: str,
        payload: dict[str, Any],
    ) -> Result:
        run = self._restore_completed_ozon_collection_if_valid(
            self.repo.load_run(run_id)
        )
        status = WorkbenchState(run["status"])
        recapture_seed_ids = {
            str(value).strip()
            for value in run.get("supplier_recapture_seed_ids") or []
            if str(value).strip()
        }
        requested_seed_id = str(payload.get("seed_id") or "").strip()
        is_late_recapture = (
            status in {WorkbenchState.SUPPLIER_COLLECTED, WorkbenchState.IMAGE_PROCESSING}
            and requested_seed_id in recapture_seed_ids
        )
        if status != WorkbenchState.SUPPLIER_REVIEW and not is_late_recapture:
            return Result.failure(
                "supplier_selection.not_expected",
                "A managed 1688 product can only be captured during supplier review.",
                data={"run_id": run_id, "status": run["status"]},
            )
        review = self.repo.load_supplier_review(run_id)
        expected_dispatch_token = str(
            run.get("browser_task_resumed_at")
            or review.get("created_at")
            or run.get("created_at")
            or ""
        ).strip()
        payload_dispatch_token = str(payload.get("dispatch_token") or "").strip()
        if not payload_dispatch_token or payload_dispatch_token != expected_dispatch_token:
            return Result.failure(
                "supplier_selection.dispatch_stale",
                "This 1688 tab belongs to an outdated supplier-selection dispatch.",
                data={
                    "run_id": run_id,
                    "status": run["status"],
                    "received_dispatch_token": payload_dispatch_token or None,
                },
            )
        items = [item for item in review.get("items", []) if isinstance(item, dict)]
        seed_id = requested_seed_id
        ozon_product_id = str(payload.get("ozon_product_id") or "").strip()
        try:
            channel_index = int(payload.get("channel_index"))
        except (TypeError, ValueError):
            channel_index = -1
        if channel_index < 0 or channel_index >= len(items):
            return Result.failure(
                "supplier_selection.channel_mismatch",
                "The managed 1688 tab is not bound to this review channel.",
                data={"run_id": run_id, "channel_index": channel_index, "seed_id": seed_id},
            )
        expected_item = items[channel_index]
        if (
            str(expected_item.get("seed_id") or "") != seed_id
            or str(expected_item.get("ozon_product_id") or "") != ozon_product_id
        ):
            return Result.failure(
                "supplier_selection.channel_mismatch",
                "The managed 1688 tab no longer matches its Ozon product channel.",
                data={"run_id": run_id, "channel_index": channel_index, "seed_id": seed_id},
            )
        raw_product = payload.get("supplier_product")
        if not isinstance(raw_product, dict):
            return Result.failure(
                "supplier_selection.product_required",
                "The current 1688 detail page did not return a supplier product.",
                data={"run_id": run_id, "seed_id": seed_id},
            )
        product = dict(raw_product)
        supplier_url = str(product.get("supplier_url") or "").strip()
        if not self._is_1688_product_url(supplier_url):
            return Result.failure(
                "supplier_selection.detail_url_required",
                "Open an exact 1688 product detail page before collecting.",
                data={"run_id": run_id, "seed_id": seed_id, "supplier_url": supplier_url},
            )
        supplier_offer_match = re.search(r"/offer/(\d+)\.html", supplier_url)
        supplier_offer_id = supplier_offer_match.group(1) if supplier_offer_match else ""
        final_url = str(product.get("final_url") or "").strip()
        final_offer_match = re.search(r"/offer/(\d+)\.html", final_url) if final_url else None
        final_offer_id = final_offer_match.group(1) if final_offer_match else ""
        reported_offer_id = str(
            product.get("offer_id")
            or product.get("supplier_product_id")
            or ""
        ).strip()
        offer_ids = {
            value
            for value in (supplier_offer_id, final_offer_id, reported_offer_id)
            if value
        }
        if (
            not supplier_offer_id
            or not reported_offer_id
            or (final_url and not final_offer_id)
            or len(offer_ids) != 1
        ):
            return Result.failure(
                "supplier_selection.offer_identity_mismatch",
                "The 1688 URL, final page and captured Offer ID do not identify one product.",
                data={
                    "run_id": run_id,
                    "seed_id": seed_id,
                    "supplier_url": supplier_url,
                    "final_url": final_url or None,
                    "reported_offer_id": reported_offer_id or None,
                },
            )
        product["seed_id"] = seed_id
        product["supplier_url"] = supplier_url
        product["offer_id"] = supplier_offer_id
        if is_late_recapture:
            missing = self._supplier_product_missing_fields(product)
            if missing:
                return Result.failure(
                    "supplier_selection.product_incomplete",
                    "The re-collected 1688 product is still missing required public fields.",
                    errors=[f"{seed_id}: required public field is missing: {field}." for field in missing],
                    data={"run_id": run_id, "seed_id": seed_id, "status": run["status"]},
                )
            product, _option_errors = self._normalize_supplier_sku_matrix(
                product,
                allow_deferred_sku=True,
            )

        draft_path = self.repo.run_dir(run_id) / "supplier_selection_draft.json"
        draft = self.repo.load_supplier_selection_draft(run_id) if draft_path.exists() else {
            "schema_version": 1,
            "run_id": run_id,
            "supplier_products": [],
            "created_at": utc_now_iso(),
        }
        persisted_products = [
            item
            for item in draft.get("supplier_products", [])
            if isinstance(item, dict)
        ]
        if is_late_recapture:
            persisted_products.extend(
                item
                for item in self.repo.load_supplier_collection_result(run_id).get(
                    "supplier_products",
                    [],
                )
                if isinstance(item, dict)
            )
        review_by_seed = {
            str(item.get("seed_id") or ""): item
            for item in items
            if isinstance(item, dict)
        }
        for existing in persisted_products:
            existing_seed_id = str(existing.get("seed_id") or "").strip()
            if not existing_seed_id or existing_seed_id == seed_id:
                continue
            existing_offer_id = str(
                existing.get("offer_id")
                or existing.get("supplier_product_id")
                or ""
            ).strip()
            if not existing_offer_id:
                existing_match = re.search(
                    r"/offer/(\d+)\.html",
                    str(existing.get("supplier_url") or ""),
                )
                existing_offer_id = (
                    existing_match.group(1) if existing_match else ""
                )
            if existing_offer_id != supplier_offer_id:
                continue
            conflicting_review = review_by_seed.get(existing_seed_id) or {}
            return Result.failure(
                "supplier_selection.offer_already_assigned",
                "This 1688 Offer is already assigned to another Ozon product in the batch.",
                data={
                    "run_id": run_id,
                    "seed_id": seed_id,
                    "offer_id": supplier_offer_id,
                    "conflicting_seed_id": existing_seed_id,
                    "conflicting_ozon_product_id": conflicting_review.get(
                        "ozon_product_id"
                    ),
                },
            )
        offer_conflicts = self.repo.claim_product_identities(
            run_id,
            [
                {
                    "identity_type": "supplier_offer_id",
                    "identity_value": supplier_offer_id,
                    "seed_id": seed_id,
                    "source": "supplier_selection_capture",
                }
            ],
        )
        if offer_conflicts:
            previous_run_ids = {
                str(item.get("previous_run_id") or "") for item in offer_conflicts
            }
            cross_batch = any(previous_run_id != run_id for previous_run_id in previous_run_ids)
            code = (
                "supplier_selection.offer_used_by_previous_batch"
                if cross_batch
                else "supplier_selection.offer_already_assigned"
            )
            message = (
                "This 1688 Offer was already used by a previous batch and cannot be selected again."
                if cross_batch
                else "This 1688 Offer is already assigned to another product in the batch."
            )
            return Result.failure(
                code,
                message,
                data={
                    "run_id": run_id,
                    "seed_id": seed_id,
                    "offer_id": supplier_offer_id,
                    "conflicts": offer_conflicts,
                },
            )
        captured = {
            str(item.get("seed_id") or ""): item
            for item in draft.get("supplier_products", [])
            if isinstance(item, dict)
        }
        captured[seed_id] = product
        ordered_products = [captured[str(item.get("seed_id") or "")] for item in items if str(item.get("seed_id") or "") in captured]
        draft["supplier_products"] = ordered_products
        draft["updated_at"] = utc_now_iso()
        self.repo.save_supplier_selection_draft(run_id, draft)

        expected_item["supplier_url"] = supplier_url
        expected_item["user_verified_exact_match"] = True
        expected_item["verified_at"] = utc_now_iso()
        review["items"] = items
        review["updated_at"] = utc_now_iso()
        self.repo.save_supplier_review(run_id, review)
        captured_count = len(ordered_products)
        total_count = len(items)
        self.repo.append_run_event(
            run_id,
            "supplier_selection.product_captured",
            "One user-confirmed 1688 product was captured from its managed browser channel.",
            {"seed_id": seed_id, "channel_index": channel_index, "captured_count": captured_count, "total_count": total_count},
        )
        if is_late_recapture:
            collection = self.repo.load_supplier_collection_result(run_id)
            products_by_seed = {
                str(item.get("seed_id") or ""): dict(item)
                for item in collection.get("supplier_products", [])
                if isinstance(item, dict) and str(item.get("seed_id") or "")
            }
            products_by_seed[seed_id] = product
            ordered_seed_ids = [
                str(item.get("seed_id") or "")
                for item in items
                if str(item.get("seed_id") or "")
            ]
            ordered_products = [
                products_by_seed[item_seed_id]
                for item_seed_id in ordered_seed_ids
                if item_seed_id in products_by_seed
            ]
            ordered_products.extend(
                item
                for item_seed_id, item in products_by_seed.items()
                if item_seed_id not in ordered_seed_ids
            )
            collection["supplier_products"] = ordered_products
            collection["updated_at"] = utc_now_iso()
            self.repo.save_supplier_collection_result(run_id, collection)
            run["supplier_recapture_seed_ids"] = sorted(recapture_seed_ids - {seed_id})
            run["browser_task_cancelled"] = False
            self.repo.save_run(run)
            event = self.repo.append_run_event(
                run_id,
                "supplier_selection.recapture_complete",
                "One incomplete supplier product was re-collected without rolling back the batch stage.",
                {
                    "seed_id": seed_id,
                    "sku_matrix_status": product.get("sku_matrix_status"),
                    "remaining_recapture_count": len(run["supplier_recapture_seed_ids"]),
                },
            )
            return Result.success(
                "supplier_selection.recapture_complete",
                "The supplier product was re-collected and is ready for SKU confirmation.",
                self._response_payload(
                    run,
                    event,
                    {
                        "seed_id": seed_id,
                        "accepted": True,
                        "lane_terminal": "collected",
                        "sku_matrix_status": product.get("sku_matrix_status"),
                    },
                ),
            )
        if captured_count < total_count:
            return Result.success(
                "supplier_selection.product_captured",
                "The current 1688 product was saved. Other managed channels are still waiting.",
                {
                    "run_id": run_id,
                    "status": run["status"],
                    "captured_count": captured_count,
                    "total_count": total_count,
                    "accepted": True,
                    "lane_terminal": "collected",
                },
            )

        started = self.start_supplier_collection(run_id, allow_user_confirmed_partial_sku=True)
        if not started.ok:
            return started
        ingested = self.ingest_supplier_collection_result(
            run_id,
            {
                "run_id": run_id,
                "worker": "workbench_browser_bridge",
                "source": "1688_managed_user_confirmed_page",
                "network": {"mode": "direct", "proxy_disabled": True},
                "supplier_products": ordered_products,
            },
        )
        if not ingested.ok:
            return ingested
        data = dict(ingested.data or {})
        data.update(
            {
                "captured_count": captured_count,
                "total_count": total_count,
                "accepted": True,
                "lane_terminal": "collected",
            }
        )
        return Result.success(
            "supplier_selection.batch_complete",
            "All managed 1688 channels were collected and validated.",
            data,
        )

    def reset_supplier_selection_product(self, run_id: str, seed_id: str) -> Result:
        run = self.repo.load_run(run_id)
        status = WorkbenchState(run["status"])
        if status not in {
            WorkbenchState.SUPPLIER_REVIEW,
            WorkbenchState.SUPPLIER_COLLECTED,
            WorkbenchState.IMAGE_PROCESSING,
        }:
            return Result.failure(
                "supplier_selection.reset_not_allowed",
                "A captured supplier can only be reset before upload begins.",
                data={"run_id": run_id, "status": run["status"], "seed_id": seed_id},
            )
        review = self.repo.load_supplier_review(run_id)
        items = [item for item in review.get("items", []) if isinstance(item, dict)]
        selected = next((item for item in items if str(item.get("seed_id") or "") == seed_id), None)
        if selected is None:
            return Result.failure(
                "supplier_selection.seed_missing",
                "The requested supplier-selection lane is not part of this review.",
                data={"run_id": run_id, "seed_id": seed_id},
            )

        if status in {WorkbenchState.SUPPLIER_COLLECTED, WorkbenchState.IMAGE_PROCESSING}:
            selection_path = self.repo.run_dir(run_id) / "supplier_sku_selections.json"
            selections = self.repo.load_supplier_sku_selections(run_id) if selection_path.exists() else {}
            if isinstance(selections.get("selections", {}).get(seed_id), dict):
                return Result.failure(
                    "supplier_selection.reset_locked",
                    "Reopen the confirmed supplier SKU before re-collecting this product.",
                    data={"run_id": run_id, "status": run["status"], "seed_id": seed_id},
                )
            subject_path = self.repo.run_dir(run_id) / "subject_masters.json"
            subjects = self.repo.load_subject_masters(run_id) if subject_path.exists() else {}
            if isinstance(subjects.get("items", {}).get(seed_id), dict):
                return Result.failure(
                    "supplier_selection.reset_locked",
                    "This product already has locked subject evidence and cannot be re-collected directly.",
                    data={"run_id": run_id, "status": run["status"], "seed_id": seed_id},
                )

        selected["supplier_url"] = None
        selected["user_verified_exact_match"] = False
        selected["verified_at"] = None
        review["items"] = items
        review["updated_at"] = utc_now_iso()
        self.repo.save_supplier_review(run_id, review)

        draft_path = self.repo.run_dir(run_id) / "supplier_selection_draft.json"
        retained_count = 0
        if draft_path.exists():
            draft = self.repo.load_supplier_selection_draft(run_id)
            retained = [
                product
                for product in draft.get("supplier_products", [])
                if isinstance(product, dict) and str(product.get("seed_id") or "") != seed_id
            ]
            draft["supplier_products"] = retained
            draft["updated_at"] = utc_now_iso()
            self.repo.save_supplier_selection_draft(run_id, draft)
            retained_count = len(retained)

        if status in {WorkbenchState.SUPPLIER_COLLECTED, WorkbenchState.IMAGE_PROCESSING}:
            collection_path = self.repo.run_dir(run_id) / "supplier_collection_result.json"
            if collection_path.exists():
                collection = self.repo.load_supplier_collection_result(run_id)
                retained_products = [
                    product
                    for product in collection.get("supplier_products", [])
                    if isinstance(product, dict) and str(product.get("seed_id") or "") != seed_id
                ]
                collection["supplier_products"] = retained_products
                collection["updated_at"] = utc_now_iso()
                self.repo.save_supplier_collection_result(run_id, collection)
                retained_count = len(retained_products)
            pending = {
                str(value).strip()
                for value in run.get("supplier_recapture_seed_ids") or []
                if str(value).strip()
            }
            pending.add(seed_id)
            run["supplier_recapture_seed_ids"] = sorted(pending)
            self.repo.save_run(run)

        event = self.repo.append_run_event(
            run_id,
            "supplier_selection.recapture_requested",
            "The saved supplier selection was cleared so its exact managed lane can be collected again.",
            {"seed_id": seed_id, "retained_count": retained_count},
        )
        return Result.success(
            "supplier_selection.recapture_requested",
            "The saved supplier selection was cleared and is pending collection again.",
            self._response_payload(
                run,
                event,
                {"seed_id": seed_id, "retained_count": retained_count},
            ),
        )

    def reject_supplier_candidate(
        self,
        run_id: str,
        seed_id: str,
        reason: str,
        random_seed: int | None = None,
    ) -> Result:
        run = self.repo.load_run(run_id)
        if WorkbenchState(run["status"]) != WorkbenchState.SUPPLIER_REVIEW:
            return Result.failure(
                "supplier_review.not_expected",
                "A product can only be rejected during supplier review.",
                data={"run_id": run_id, "status": run["status"]},
            )
        review = self.repo.load_supplier_review(run_id)
        review_item = next(
            (item for item in review.get("items", []) if str(item.get("seed_id") or "") == seed_id),
            None,
        )
        if review_item is None:
            return Result.failure(
                "supplier_review.seed_not_found",
                "The selected product no longer belongs to this supplier review.",
                data={"run_id": run_id, "seed_id": seed_id},
            )
        ozon_product_id = str(review_item.get("ozon_product_id") or "").strip()
        if not ozon_product_id:
            return Result.failure(
                "supplier_review.ozon_product_id_missing",
                "The selected product has no Ozon product id and cannot be blacklisted safely.",
                data={"run_id": run_id, "seed_id": seed_id},
            )
        sampled = self.repo.load_sampled_seeds(run_id)
        rejected_seed = next((seed for seed in sampled if seed.seed_id == seed_id), None)
        if rejected_seed is None:
            return Result.failure(
                "supplier_review.seed_not_sampled",
                "The selected seed no longer belongs to this batch.",
                data={"run_id": run_id, "seed_id": seed_id},
            )

        rejected_attempts = self.repo.load_rejected_seed_attempts(run_id)
        excluded_ids = (
            self.repo.load_used_seed_ids()
            | self.repo.load_blacklisted_seed_ids()
            | {seed.seed_id for seed in sampled}
            | {str(item.get("seed_id")) for item in rejected_attempts if item.get("seed_id")}
        )
        excluded_identity_keys = (
            self.repo.load_used_seed_identity_keys()
            | self.repo.load_blacklisted_seed_identity_keys()
            | {self.repo.seed_identity_key(seed) for seed in sampled}
            | {
                self.repo.seed_identity_key(item.get("seed") or str(item.get("seed_id") or ""))
                for item in rejected_attempts
                if item.get("seed") or item.get("seed_id")
            }
        )
        existing_products = self.repo.load_existing_products()
        eligible = [
            seed
            for seed in self.repo.load_active_seeds()
            if seed.seed_id not in excluded_ids
            and self.repo.seed_identity_key(seed) not in excluded_identity_keys
            and decide_seed_existing_product_dedupe(seed, existing_products).kind.value == "clear"
        ]
        if not eligible:
            return Result.failure(
                "supplier_review.no_replacement_seed",
                "No eligible replacement seed remains after blacklist and store dedupe filtering.",
                data={"run_id": run_id, "seed_id": seed_id},
            )
        selected_random_seed = random_seed if random_seed is not None else random.SystemRandom().randint(1, 2**31 - 1)
        replacement = self.repo.sample_seeds(eligible, 1, selected_random_seed)[0]

        try:
            template_payload = self.repo.load_attribute_template_result(run_id)
        except FileNotFoundError:
            template_payload = {"run_id": run_id, "seed_templates": []}
        try:
            ozon_payload = self.repo.load_ozon_collection_result(run_id)
        except FileNotFoundError:
            ozon_payload = {"run_id": run_id, "ozon_candidates": []}
        rejected_template = next(
            (item for item in template_payload.get("seed_templates", []) if str(item.get("seed_id") or "") == seed_id),
            None,
        )
        rejected_candidate = next(
            (item for item in ozon_payload.get("ozon_candidates", []) if str(item.get("seed_id") or "") == seed_id),
            None,
        )
        self.repo.append_supplier_rejection(
            run_id,
            {
                "seed": rejected_seed.to_dict(),
                "seed_id": seed_id,
                "ozon_product_id": ozon_product_id,
                "reason_code": "supplier_not_found_by_user",
                "reason": reason,
                "replacement_seed_id": replacement.seed_id,
                "supplier_review_item": review_item,
                "attribute_template_evidence": rejected_template,
                "ozon_candidate_evidence": rejected_candidate,
            },
        )
        self.repo.append_seed_blacklist(run_id, rejected_seed, ozon_product_id, reason)
        self.repo.append_rejected_seed_attempt(run_id, rejected_seed, reason, replacement.seed_id)
        replacement_slot = self._replace_candidate_slot(
            run,
            rejected_seed_id=seed_id,
            replacement_seed_id=replacement.seed_id,
            rejected_ozon_product_id=ozon_product_id,
            reason=reason,
        )

        sampled[next(index for index, seed in enumerate(sampled) if seed.seed_id == seed_id)] = replacement
        self.repo.save_sampled_seeds(run_id, sampled)
        self.repo.append_used_seeds(run_id, [replacement], "workbench_replacement_sampled")
        template_payload["seed_templates"] = [
            item for item in template_payload.get("seed_templates", []) if str(item.get("seed_id") or "") != seed_id
        ]
        self.repo.save_attribute_template_result(run_id, template_payload)
        ozon_payload["ozon_candidates"] = [
            item for item in ozon_payload.get("ozon_candidates", []) if str(item.get("seed_id") or "") != seed_id
        ]
        self.repo.save_ozon_collection_result(run_id, ozon_payload)
        (self.repo.run_dir(run_id) / "ozon_collection_draft.json").unlink(missing_ok=True)
        self._prune_excluded_product_artifacts(run_id, seed_id)
        review["items"] = [
            item for item in review.get("items", []) if str(item.get("seed_id") or "") != seed_id
        ]
        review["updated_at"] = utc_now_iso()
        self.repo.save_supplier_review(run_id, review)

        run["sampled_seed_ids"] = [seed.seed_id for seed in sampled]
        run["replacement_pending_seed_ids"] = [replacement.seed_id]
        run["status"] = WorkbenchState.SEED_SELECTED.value
        run["attribute_template_contract_ready"] = False
        run["attribute_template_collected"] = False
        run["ozon_collection_contract_ready"] = False
        run["ozon_collected"] = False
        run["browser_task_cancelled"] = False
        for filename in (
            "attribute_template_contract.json",
            "ozon_collection_contract.json",
        ):
            (self.repo.run_dir(run_id) / filename).unlink(missing_ok=True)
        for key in ("attribute_template_contract_path", "ozon_collection_contract_path"):
            run.pop(key, None)
        self._update_query_summary(run, sampled)
        self.repo.save_run(run)
        event = self.repo.append_run_event(
            run_id,
            "supplier_review.candidate_replaced",
            "A product without an exact 1688 supplier was blacklisted and replaced in the batch queue.",
            {
                "rejected_seed_id": seed_id,
                "rejected_ozon_product_id": ozon_product_id,
                "replacement_seed_id": replacement.seed_id,
                "slot_id": replacement_slot["slot_id"],
                "candidate_revision": replacement_slot["candidate_revision"],
                "random_seed": selected_random_seed,
            },
        )
        return Result.success(
            "supplier_review.candidate_replaced",
            "The rejected product was blacklisted and the batch was refilled with one replacement seed.",
            self._response_payload(
                run,
                event,
                {"rejected_seed_id": seed_id, "replacement_seed": replacement.to_dict()},
            ),
        )

    def exclude_product_without_replacement(
        self,
        run_id: str,
        seed_id: str,
        reason: str,
        *,
        confirmed: bool = False,
    ) -> Result:
        with self._run_mutation_lock(run_id):
            return self._exclude_product_without_replacement(
                run_id,
                seed_id,
                reason,
                confirmed=confirmed,
            )

    def _exclude_product_without_replacement(
        self,
        run_id: str,
        seed_id: str,
        reason: str,
        *,
        confirmed: bool,
    ) -> Result:
        if confirmed is not True:
            return Result.failure(
                "product_exclusion.confirmation_required",
                "Explicit confirmation is required before permanently excluding a product.",
                data={"run_id": run_id, "seed_id": seed_id},
            )
        run = self.repo.load_run(run_id)
        current_state = WorkbenchState(run["status"])
        if current_state not in {
            WorkbenchState.SUPPLIER_COLLECTED,
            WorkbenchState.IMAGE_PROCESSING,
        }:
            return Result.failure(
                "product_exclusion.invalid_state",
                "A product can only be excluded after supplier collection and before upload.",
                data={"run_id": run_id, "seed_id": seed_id, "status": run["status"]},
            )

        sampled = self.repo.load_sampled_seeds(run_id)
        excluded_seed = next(
            (seed for seed in sampled if seed.seed_id == seed_id),
            None,
        )
        if excluded_seed is None:
            return Result.failure(
                "product_exclusion.seed_not_sampled",
                "The selected product is no longer active in this batch.",
                data={"run_id": run_id, "seed_id": seed_id},
            )

        submissions_path = self.repo.run_dir(run_id) / "upload_submissions.json"
        if submissions_path.exists():
            submissions = self.repo.load_upload_submissions(run_id)
            submission = (submissions.get("items") or {}).get(seed_id)
            submission_status = str(
                (submission or {}).get("status") or ""
            ).strip().casefold()
            if submission_status and submission_status not in {
                "failed",
                "error",
                "declined",
            }:
                return Result.failure(
                    "product_exclusion.upload_already_started",
                    "This product has already been submitted to Ozon and cannot be removed from the local batch.",
                    data={
                        "run_id": run_id,
                        "seed_id": seed_id,
                        "submission_status": submission_status,
                    },
                )

        try:
            ozon_payload = self.repo.load_ozon_collection_result(run_id)
        except FileNotFoundError:
            return Result.failure(
                "product_exclusion.ozon_evidence_missing",
                "Ozon evidence is required before a product can be blacklisted safely.",
                data={"run_id": run_id, "seed_id": seed_id},
            )
        excluded_candidate = next(
            (
                item
                for item in ozon_payload.get("ozon_candidates", [])
                if str(item.get("seed_id") or "") == seed_id
            ),
            None,
        )
        ozon_product_id = str(
            (excluded_candidate or {}).get("ozon_product_id") or ""
        ).strip()
        if not ozon_product_id:
            return Result.failure(
                "product_exclusion.ozon_product_id_missing",
                "The selected product has no Ozon product id and cannot be blacklisted safely.",
                data={"run_id": run_id, "seed_id": seed_id},
            )

        normalized_reason = str(reason or "").strip() or (
            "User marked the product as unsuitable for cross-border sale."
        )
        template_payload = self.repo.load_attribute_template_result(run_id)
        excluded_template = next(
            (
                item
                for item in template_payload.get("seed_templates", [])
                if str(item.get("seed_id") or "") == seed_id
            ),
            None,
        )
        supplier_product = None
        supplier_path = self.repo.run_dir(run_id) / "supplier_collection_result.json"
        if supplier_path.exists():
            supplier_payload = self.repo.load_supplier_collection_result(run_id)
            supplier_product = next(
                (
                    item
                    for item in supplier_payload.get("supplier_products", [])
                    if str(item.get("seed_id") or "") == seed_id
                ),
                None,
            )
        selection = None
        selection_path = self.repo.run_dir(run_id) / "supplier_sku_selections.json"
        if selection_path.exists():
            selection = (
                self.repo.load_supplier_sku_selections(run_id).get("selections")
                or {}
            ).get(seed_id)

        self.repo.append_supplier_rejection(
            run_id,
            {
                "seed": excluded_seed.to_dict(),
                "seed_id": seed_id,
                "ozon_product_id": ozon_product_id,
                "reason_code": "cross_border_unsuitable_by_user",
                "reason": normalized_reason,
                "replacement_seed_id": None,
                "attribute_template_evidence": excluded_template,
                "ozon_candidate_evidence": excluded_candidate,
                "supplier_product_evidence": supplier_product,
                "supplier_selection_evidence": selection,
            },
        )
        self.repo.append_seed_blacklist(
            run_id,
            excluded_seed,
            ozon_product_id,
            normalized_reason,
            reason_code="cross_border_unsuitable_by_user",
        )
        self.repo.append_rejected_seed_attempt(
            run_id,
            excluded_seed,
            normalized_reason,
            replacement_seed_id=None,
        )

        remaining = [seed for seed in sampled if seed.seed_id != seed_id]
        self.repo.save_sampled_seeds(run_id, remaining)
        template_payload["seed_templates"] = [
            item
            for item in template_payload.get("seed_templates", [])
            if str(item.get("seed_id") or "") != seed_id
        ]
        template_payload["updated_at"] = utc_now_iso()
        self.repo.save_attribute_template_result(run_id, template_payload)
        ozon_payload["ozon_candidates"] = [
            item
            for item in ozon_payload.get("ozon_candidates", [])
            if str(item.get("seed_id") or "") != seed_id
        ]
        ozon_payload["updated_at"] = utc_now_iso()
        self.repo.save_ozon_collection_result(run_id, ozon_payload)
        self._prune_excluded_product_artifacts(run_id, seed_id)

        run["sampled_seed_ids"] = [seed.seed_id for seed in remaining]
        run["active_product_count"] = len(remaining)
        run["excluded_seed_ids"] = sorted(
            {
                str(value)
                for value in run.get("excluded_seed_ids") or []
                if str(value)
            }
            | {seed_id}
        )
        run["removed_seed_ids"] = sorted(
            {
                str(value)
                for value in run.get("removed_seed_ids") or []
                if str(value)
            }
            | {seed_id}
        )
        replacement_pending = [
            str(value)
            for value in run.get("replacement_pending_seed_ids") or []
            if str(value) and str(value) != seed_id
        ]
        if replacement_pending:
            run["replacement_pending_seed_ids"] = replacement_pending
        else:
            run.pop("replacement_pending_seed_ids", None)
        self._update_query_summary(run, remaining)
        self.repo.save_run(run)
        event = self.repo.append_run_event(
            run_id,
            "product_exclusion.completed",
            "A cross-border unsuitable product was permanently blacklisted and removed without refill.",
            {
                "excluded_seed_id": seed_id,
                "excluded_ozon_product_id": ozon_product_id,
                "remaining_product_count": len(remaining),
                "replacement_seed_id": None,
                "reason": normalized_reason,
            },
        )
        return Result.success(
            "product_exclusion.completed",
            "The product was removed without refill and permanently added to the seed blacklist.",
            self._response_payload(
                run,
                event,
                {
                    "excluded_seed_id": seed_id,
                    "excluded_ozon_product_id": ozon_product_id,
                    "remaining_product_count": len(remaining),
                    "replacement_seed_id": None,
                },
            ),
        )

    def _prune_excluded_product_artifacts(
        self,
        run_id: str,
        seed_id: str,
    ) -> None:
        list_artifacts = (
            (
                "supplier_review.json",
                self.repo.load_supplier_review,
                self.repo.save_supplier_review,
                "items",
            ),
            (
                "supplier_selection_draft.json",
                self.repo.load_supplier_selection_draft,
                self.repo.save_supplier_selection_draft,
                "supplier_products",
            ),
            (
                "supplier_collection_result.json",
                self.repo.load_supplier_collection_result,
                self.repo.save_supplier_collection_result,
                "supplier_products",
            ),
        )
        for filename, loader, saver, key in list_artifacts:
            if not (self.repo.run_dir(run_id) / filename).exists():
                continue
            payload = loader(run_id)
            payload[key] = [
                item
                for item in payload.get(key, [])
                if str(item.get("seed_id") or "") != seed_id
            ]
            payload["updated_at"] = utc_now_iso()
            saver(run_id, payload)

        mapping_artifacts = (
            (
                "supplier_sku_selections.json",
                self.repo.load_supplier_sku_selections,
                self.repo.save_supplier_sku_selections,
                "selections",
            ),
            (
                "subject_masters.json",
                self.repo.load_subject_masters,
                self.repo.save_subject_masters,
                "items",
            ),
            (
                "pricing_evidence.json",
                self.repo.load_pricing_evidence,
                self.repo.save_pricing_evidence,
                "items",
            ),
            (
                "generated_content_result.json",
                self.repo.load_generated_content_result,
                self.repo.save_generated_content_result,
                "items",
            ),
            (
                "required_attribute_evidence.json",
                self.repo.load_required_attribute_evidence,
                self.repo.save_required_attribute_evidence,
                "items",
            ),
            (
                "upload_previews.json",
                self.repo.load_upload_previews,
                self.repo.save_upload_previews,
                "items",
            ),
            (
                "upload_submissions.json",
                self.repo.load_upload_submissions,
                self.repo.save_upload_submissions,
                "items",
            ),
        )
        for filename, loader, saver, key in mapping_artifacts:
            if not (self.repo.run_dir(run_id) / filename).exists():
                continue
            payload = loader(run_id)
            values = dict(payload.get(key) or {})
            values.pop(seed_id, None)
            payload[key] = values
            payload["updated_at"] = utc_now_iso()
            saver(run_id, payload)

    def start_supplier_collection(
        self,
        run_id: str,
        *,
        allow_user_confirmed_partial_sku: bool = False,
    ) -> Result:
        run = self.repo.load_run(run_id)
        if WorkbenchState(run["status"]) != WorkbenchState.SUPPLIER_REVIEW:
            return Result.failure(
                "supplier_review.not_expected",
                "Supplier collection can only start from supplier review.",
                data={"run_id": run_id, "status": run["status"]},
            )
        review = self.repo.load_supplier_review(run_id)
        if not self._supplier_review_complete(review):
            return Result.failure(
                "supplier_review.links_required",
                "Every Ozon product needs a user-verified 1688 product link before collection starts.",
                data={"run_id": run_id, "status": run["status"]},
            )
        required_fields = [
            "title",
            "seller",
            "sku",
            "images",
            "domestic_shipping_evidence",
        ]
        if not allow_user_confirmed_partial_sku:
            required_fields.insert(3, "sku_options")
        contract = {
            "run_id": run_id,
            "contract_type": "supplier_collection",
            "network": {"mode": "direct", "proxy_disabled": True},
            "items": review.get("items", []),
            "rules": {
                "user_verified_exact_match": True,
                "collect_supplied_url_only": True,
                "public_data_only": True,
                "required_fields": required_fields,
                "allow_user_confirmed_partial_sku": allow_user_confirmed_partial_sku,
                "sku_confirmation_gate": (
                    "after_public_evidence_capture"
                    if allow_user_confirmed_partial_sku
                    else "complete_matrix_required"
                ),
            },
            "created_at": utc_now_iso(),
        }
        contract_path = self.repo.save_supplier_collection_contract(run_id, contract)
        run["supplier_collection_contract_path"] = str(contract_path)
        run["browser_task_cancelled"] = False
        run["browser_task_resumed_at"] = utc_now_iso()
        run["status"] = transition_workbench_state(
            WorkbenchState(run["status"]), WorkbenchAction.START_SUPPLIER_COLLECTION
        ).value
        self.repo.save_run(run)
        event = self.repo.append_run_event(
            run_id,
            "supplier_collection.contract_ready",
            "Direct-network 1688 supplier collection contract is ready.",
            {"contract_path": str(contract_path), "item_count": len(contract["items"])},
        )
        return Result.success(
            "supplier_collection.contract_ready",
            "Direct-network 1688 supplier collection started.",
            self._response_payload(run, event, {"contract": contract}),
        )

    def ingest_supplier_collection_result(self, run_id: str, payload: dict[str, Any]) -> Result:
        run = self.repo.load_run(run_id)
        if WorkbenchState(run["status"]) != WorkbenchState.SUPPLIER_COLLECTING:
            return Result.failure(
                "supplier_collection.not_expected",
                "Supplier collection results are only accepted during supplier collection.",
                data={"run_id": run_id, "status": run["status"]},
            )
        contract = self.repo.load_supplier_collection_contract(run_id)
        rules = contract.get("rules") if isinstance(contract.get("rules"), dict) else {}
        allow_deferred_sku = (
            rules.get("allow_user_confirmed_partial_sku") is True
            and str(payload.get("source") or "") == "1688_managed_user_confirmed_page"
        )
        expected = {
            str(item.get("seed_id")): str(item.get("supplier_url"))
            for item in contract.get("items", [])
        }
        products = payload.get("supplier_products") if isinstance(payload.get("supplier_products"), list) else []
        errors: list[str] = []
        collected: dict[str, dict[str, Any]] = {}
        if payload.get("network", {}).get("proxy_disabled") is not True:
            errors.append("Supplier collection must explicitly confirm that proxy was disabled.")
        for raw_product in products:
            if not isinstance(raw_product, dict):
                errors.append("A supplier product must be an object.")
                continue
            product = dict(raw_product)
            seed_id = str(product.get("seed_id") or "")
            if seed_id not in expected:
                errors.append(f"Unexpected supplier seed_id: {seed_id or '(missing)'}")
                continue
            if str(product.get("supplier_url") or "") != expected[seed_id]:
                errors.append(f"{seed_id}: collected URL does not match the user-verified link.")
            required = {
                "title": product.get("title"),
                "seller": product.get("seller"),
                "sku": product.get("sku"),
                "images": product.get("images"),
                "domestic_shipping_evidence": product.get("domestic_shipping_evidence"),
            }
            if not allow_deferred_sku:
                required["sku_options"] = product.get("sku_options")
            for field, value in required.items():
                if value in (None, "", [], {}):
                    errors.append(f"{seed_id}: required public field is missing: {field}.")

            product, option_errors = self._normalize_supplier_sku_matrix(
                product,
                allow_deferred_sku=allow_deferred_sku,
            )
            errors.extend(f"{seed_id}: {error}." for error in option_errors)
            collected[seed_id] = product
        for seed_id in expected:
            if seed_id not in collected:
                errors.append(f"{seed_id}: supplier product was not collected.")
        if errors:
            event = self.repo.append_run_event(
                run_id,
                "supplier_collection.ingest_invalid",
                "1688 supplier collection result failed validation.",
                {"errors": errors},
            )
            return Result.failure(
                "supplier_collection.invalid",
                "1688 supplier collection result failed validation.",
                errors=errors,
                data=self._response_payload(run, event),
            )

        normalized_products = [collected[seed_id] for seed_id in expected]
        normalized_payload = dict(payload)
        normalized_payload["supplier_products"] = normalized_products
        result_path = self.repo.save_supplier_collection_result(run_id, normalized_payload)
        run["supplier_collection_result_path"] = str(result_path)
        run["status"] = transition_workbench_state(
            WorkbenchState(run["status"]), WorkbenchAction.MARK_SUPPLIER_COLLECTED
        ).value
        self.repo.save_run(run)
        deferred_sku_count = sum(
            1 for product in normalized_products
            if product.get("sku_matrix_status") == "manual_confirmation_required"
        )
        event = self.repo.append_run_event(
            run_id,
            "supplier_collection.ingested",
            "User-confirmed 1688 supplier public evidence was collected and persisted.",
            {
                "result_path": str(result_path),
                "item_count": len(normalized_products),
                "deferred_sku_count": deferred_sku_count,
            },
        )
        return Result.success(
            "supplier_collection.ingested",
            "User-confirmed 1688 supplier public evidence was collected and persisted.",
            self._response_payload(run, event, {"deferred_sku_count": deferred_sku_count}),
        )
    def confirm_supplier_sku(
        self,
        run_id: str,
        *,
        seed_id: str,
        supplier_sku_id: str,
        differences: list[dict[str, Any] | str] | None = None,
    ) -> Result:
        run = self.repo.load_run(run_id)
        selection_path = self.repo.run_dir(run_id) / "supplier_sku_selections.json"
        selections = self.repo.load_supplier_sku_selections(run_id) if selection_path.exists() else {
            "schema_version": 1,
            "run_id": run_id,
            "selections": {},
        }
        if seed_id in selections.get("selections", {}):
            return Result.failure(
                "supplier_sku_selection.locked",
                "The confirmed supplier SKU receipt is immutable.",
                data={"run_id": run_id, "seed_id": seed_id},
            )
        if WorkbenchState(run["status"]) not in {
            WorkbenchState.SUPPLIER_COLLECTED,
            WorkbenchState.IMAGE_PROCESSING,
        }:
            return Result.failure(
                "supplier_sku_selection.not_expected",
                "A supplier SKU can only be selected after supplier collection and before upload.",
                data={"run_id": run_id, "status": run["status"]},
            )
        supplier_product = next(
            (
                item
                for item in self.repo.load_supplier_collection_result(run_id).get("supplier_products", [])
                if isinstance(item, dict) and str(item.get("seed_id") or "") == seed_id
            ),
            None,
        )
        if supplier_product is None:
            return Result.failure(
                "supplier_sku_selection.product_missing",
                "The collected 1688 product was not found for this batch item.",
                data={"run_id": run_id, "seed_id": seed_id},
            )
        raw_option = next(
            (
                option
                for option in self._supplier_sku_options(supplier_product)
                if isinstance(option, dict) and str(option.get("supplier_sku_id") or "") == supplier_sku_id
            ),
            None,
        )
        if raw_option is None:
            return Result.failure(
                "supplier_sku_selection.option_missing",
                "The selected supplier SKU is not present in the collected real SKU matrix.",
                data={"run_id": run_id, "seed_id": seed_id, "supplier_sku_id": supplier_sku_id},
            )
        option = SupplierSkuOption.from_dict(raw_option)
        option_errors = validate_supplier_sku_option(option)
        if option_errors:
            return Result.failure(
                "supplier_sku_selection.option_invalid",
                "The selected supplier SKU does not have complete evidence.",
                errors=option_errors,
                data={"run_id": run_id, "seed_id": seed_id, "supplier_sku_id": supplier_sku_id},
            )
        ozon_product = next(
            (
                item
                for item in self.repo.load_ozon_collection_result(run_id).get("ozon_candidates", [])
                if isinstance(item, dict) and str(item.get("seed_id") or "") == seed_id
            ),
            {},
        )
        receipt = SupplierSkuSelectionReceipt.confirmed(
            run_id=run_id,
            product_id=str(ozon_product.get("ozon_product_id") or seed_id),
            supplier_offer_id=str(
                supplier_product.get("offer_id")
                or supplier_product.get("supplier_product_id")
                or ""
            ),
            supplier_sku=option,
            ozon_target_sku=dict(ozon_product.get("target_sku") or {}),
            differences=list(differences or []),
            confirmed_at=utc_now_iso(),
        )
        selections.setdefault("selections", {})[seed_id] = receipt.to_dict()
        selections["updated_at"] = utc_now_iso()
        saved_path = self.repo.save_supplier_sku_selections(run_id, selections)
        selection_status = self._supplier_sku_selection_status(run_id)
        event = self.repo.append_run_event(
            run_id,
            "supplier_sku_selection.confirmed",
            "One real supplier SKU was locked for this product.",
            {
                "seed_id": seed_id,
                "supplier_sku_id": supplier_sku_id,
                "selection_sha256": receipt.selection_sha256,
                "selection_path": str(saved_path),
            },
        )
        return Result.success(
            "supplier_sku_selection.confirmed",
            "The real supplier SKU selection was saved and locked.",
            self._response_payload(
                run,
                event,
                {"receipt": receipt.to_dict(), "selection_status": selection_status},
            ),
        )

    def reopen_supplier_sku_selection(self, run_id: str, *, seed_id: str) -> Result:
        run = self.repo.load_run(run_id)
        if WorkbenchState(run["status"]) not in {
            WorkbenchState.SUPPLIER_COLLECTED,
            WorkbenchState.IMAGE_PROCESSING,
        }:
            return Result.failure(
                "supplier_sku_selection.reopen_blocked",
                "A supplier SKU can only be reopened before upload begins.",
                data={"run_id": run_id, "seed_id": seed_id, "status": run["status"]},
            )

        selection_path = self.repo.run_dir(run_id) / "supplier_sku_selections.json"
        if not selection_path.exists():
            return Result.failure(
                "supplier_sku_selection.missing",
                "There is no confirmed supplier SKU to reopen.",
                data={"run_id": run_id, "seed_id": seed_id},
            )
        selections = self.repo.load_supplier_sku_selections(run_id)
        receipt = selections.get("selections", {}).get(seed_id)
        if not isinstance(receipt, dict):
            return Result.failure(
                "supplier_sku_selection.missing",
                "There is no confirmed supplier SKU to reopen.",
                data={"run_id": run_id, "seed_id": seed_id},
            )

        try:
            submissions = self.repo.load_upload_submissions(run_id)
        except FileNotFoundError:
            submissions = {}
        submission = (submissions.get("items") or {}).get(seed_id)
        if isinstance(submission, dict) and submission.get("task_id") is not None:
            return Result.failure(
                "supplier_sku_selection.reopen_blocked",
                "The product was already submitted to Ozon; its locked supplier SKU cannot be reopened.",
                data={
                    "run_id": run_id,
                    "seed_id": seed_id,
                    "seller_import_task_id": submission.get("task_id"),
                    "image_task_package_id": submission.get(
                        "image_task_package_id"
                    ),
                },
            )

        subject_path = self.repo.run_dir(run_id) / "subject_masters.json"
        subjects = self.repo.load_subject_masters(run_id) if subject_path.exists() else {
            "schema_version": 1,
            "run_id": run_id,
            "items": {},
        }
        subject_entry = subjects.get("items", {}).get(seed_id)
        job_id = str(subject_entry.get("image_job_id") or "") if isinstance(subject_entry, dict) else ""
        if isinstance(subject_entry, dict) and job_id:
            queue = self._image_generation_queue()
            if not queue.stop_unstarted(job_id):
                job = queue.get_job(job_id)
                return Result.failure(
                    "supplier_sku_selection.reopen_blocked",
                    "The image job has already started; its locked SKU evidence cannot be reopened.",
                    data={
                        "run_id": run_id,
                        "seed_id": seed_id,
                        "image_job_id": job_id,
                        "image_job_status": job.get("status") if job else "missing",
                    },
                )

        reopened_at = utc_now_iso()
        selections.setdefault("history", []).append(
            {"seed_id": seed_id, "reopened_at": reopened_at, "receipt": receipt}
        )
        selections.setdefault("selections", {}).pop(seed_id, None)
        selections["updated_at"] = reopened_at
        self.repo.save_supplier_sku_selections(run_id, selections)

        if isinstance(subject_entry, dict):
            subjects.setdefault("history", []).append(
                {"seed_id": seed_id, "reopened_at": reopened_at, "subject_entry": subject_entry}
            )
            subjects.setdefault("items", {}).pop(seed_id, None)
            subjects["updated_at"] = reopened_at
            self.repo.save_subject_masters(run_id, subjects)

        event = self.repo.append_run_event(
            run_id,
            "supplier_sku_selection.reopened",
            "The unstarted supplier SKU decision was archived and reopened for user selection.",
            {"seed_id": seed_id, "stopped_image_job_id": job_id or None},
        )
        return Result.success(
            "supplier_sku_selection.reopened",
            "The supplier SKU was reopened for selection; prior evidence remains in audit history.",
            self._response_payload(
                run,
                event,
                {
                    "seed_id": seed_id,
                    "stopped_image_job_id": job_id or None,
                    "selection_status": self._supplier_sku_selection_status(run_id),
                },
            ),
        )

    def confirm_subject_master(
        self,
        run_id: str,
        *,
        seed_id: str,
        visible_subject_quantity: int,
        source_image_urls: list[str] | None = None,
        source_image_url: str = "",
        white_background_confirmed: bool = False,
    ) -> Result:
        run = self.repo.load_run(run_id)
        subject_path = self.repo.run_dir(run_id) / "subject_masters.json"
        stored = self.repo.load_subject_masters(run_id) if subject_path.exists() else {
            "schema_version": 1,
            "run_id": run_id,
            "items": {},
        }
        if seed_id in stored.get("items", {}):
            return Result.failure(
                "subject_master.locked",
                "The confirmed subject master is immutable.",
                data={"run_id": run_id, "seed_id": seed_id},
            )
        if WorkbenchState(run["status"]) not in {
            WorkbenchState.SUPPLIER_COLLECTED,
            WorkbenchState.IMAGE_PROCESSING,
        }:
            return Result.failure(
                "subject_master.not_expected",
                "A subject master can only be locked after supplier collection and SKU selection.",
                data={"run_id": run_id, "status": run["status"]},
            )
        selection_path = self.repo.run_dir(run_id) / "supplier_sku_selections.json"
        if not selection_path.exists():
            return Result.failure(
                "subject_master.selection_missing",
                "Lock one real supplier SKU before choosing the subject master.",
                data={"run_id": run_id, "seed_id": seed_id},
            )
        raw_receipt = self.repo.load_supplier_sku_selections(run_id).get("selections", {}).get(seed_id)
        if not isinstance(raw_receipt, dict):
            return Result.failure(
                "subject_master.selection_missing",
                "Lock one real supplier SKU before choosing the subject master.",
                data={"run_id": run_id, "seed_id": seed_id},
            )
        try:
            receipt = SupplierSkuSelectionReceipt.from_dict(raw_receipt)
        except (KeyError, TypeError, ValueError) as exc:
            return Result.failure(
                "subject_master.selection_invalid",
                "The active supplier SKU receipt is invalid.",
                errors=[str(exc)],
                data={"run_id": run_id, "seed_id": seed_id},
            )

        selected_urls: list[str] = []
        raw_urls = source_image_urls if isinstance(source_image_urls, list) else [source_image_url]
        for value in raw_urls:
            url = str(value or "").strip()
            if url and url not in selected_urls:
                selected_urls.append(url)
        if not selected_urls:
            return Result.failure(
                "subject_master.images_required",
                "Select at least one supplier subject evidence image.",
                data={"run_id": run_id, "seed_id": seed_id},
            )

        supplier_product = next(
            (
                item
                for item in self.repo.load_supplier_collection_result(run_id).get(
                    "supplier_products", []
                )
                if isinstance(item, dict) and str(item.get("seed_id") or "") == seed_id
            ),
            None,
        )
        if not isinstance(supplier_product, dict):
            return Result.failure(
                "subject_master.supplier_product_missing",
                "The verified supplier product is missing.",
                data={"run_id": run_id, "seed_id": seed_id},
            )
        locked_sku_urls = {
            str(value or "").strip()
            for value in receipt.supplier_sku.image_urls
            if str(value or "").strip()
        }
        supplier_urls = self._supplier_product_images(supplier_product)
        allowed_urls = locked_sku_urls or set(supplier_urls[:1])
        foreign_urls = [url for url in selected_urls if url not in allowed_urls]
        if foreign_urls:
            supplier_url_set = set(supplier_urls)
            wrong_variant_urls = [
                url for url in foreign_urls if url in supplier_url_set
            ]
            if locked_sku_urls and wrong_variant_urls:
                return Result.failure(
                    "subject_master.image_not_in_locked_sku",
                    "Subject evidence may only use images attached to the locked supplier SKU.",
                    data={
                        "run_id": run_id,
                        "seed_id": seed_id,
                        "rejected_urls": wrong_variant_urls,
                    },
                )
            return Result.failure(
                "subject_master.image_not_in_supplier",
                "Every subject evidence image must belong to the verified supplier product.",
                data={
                    "run_id": run_id,
                    "seed_id": seed_id,
                    "rejected_urls": foreign_urls,
                },
            )
        if int(visible_subject_quantity) != receipt.supplier_sku.set_quantity:
            return Result.failure(
                "subject_master.quantity_mismatch",
                "The visible subject quantity must match the locked real supplier SKU.",
                data={
                    "run_id": run_id,
                    "seed_id": seed_id,
                    "expected_quantity": receipt.supplier_sku.set_quantity,
                    "visible_subject_quantity": int(visible_subject_quantity),
                },
            )

        safe_seed_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", seed_id).strip("._") or "item"
        target_dir = (
            self.repo.run_dir(run_id)
            / "image_generation"
            / "subject_masters"
            / safe_seed_id
        )
        try:
            downloaded_paths: list[Path] = []
            for index, url in enumerate(selected_urls, start=1):
                suffix = Path(urlparse(url).path).suffix.lower()
                if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
                    suffix = ".jpg"
                target = target_dir / f"{index:02d}{suffix}"
                downloaded_paths.append(self.supplier_image_downloader(url, target))
            subject_master = SubjectMasterSelection.create(
                receipt=receipt,
                source_paths=downloaded_paths,
                source_image_urls=selected_urls,
                visible_subject_quantity=int(visible_subject_quantity),
                white_background_confirmed=white_background_confirmed,
                confirmed_at=utc_now_iso(),
            )
        except (OSError, TypeError, ValueError) as exc:
            return Result.failure(
                "subject_master.invalid",
                "The supplier subject evidence failed the SKU truth gate.",
                errors=[str(exc)],
                data={"run_id": run_id, "seed_id": seed_id},
            )

        stored.setdefault("items", {})[seed_id] = {
            "subject_master": subject_master.to_dict(),
            "image_task_mode": "post_upload_package",
        }
        stored["updated_at"] = utc_now_iso()
        self.repo.save_subject_masters(run_id, stored)

        expected_seed_ids = {
            str(product.get("seed_id") or "")
            for product in self.repo.load_supplier_collection_result(run_id).get("supplier_products", [])
            if isinstance(product, dict) and str(product.get("seed_id") or "")
        }
        confirmed_seed_ids = set(stored.get("items", {}))
        all_confirmed = bool(expected_seed_ids) and expected_seed_ids <= confirmed_seed_ids
        if all_confirmed and WorkbenchState(run["status"]) == WorkbenchState.SUPPLIER_COLLECTED:
            run["status"] = transition_workbench_state(
                WorkbenchState(run["status"]), WorkbenchAction.START_IMAGE_PROCESSING
            ).value
            self.repo.save_run(run)

        event = self.repo.append_run_event(
            run_id,
            "subject_master.confirmed",
            "The exact supplier SKU subject evidence was locked for one-image product creation.",
            {
                "seed_id": seed_id,
                "supplier_sku_id": receipt.supplier_sku_id,
                "set_quantity": receipt.supplier_sku.set_quantity,
                "subject_master_sha256": subject_master.subject_master_sha256,
                "image_task_mode": "post_upload_package",
                "upload_stage_ready": all_confirmed,
            },
        )
        return Result.success(
            "subject_master.confirmed",
            "The exact supplier subject evidence was locked for product upload.",
            self._response_payload(
                run,
                event,
                {
                    "subject_master": subject_master.to_dict(),
                    "image_task_mode": "post_upload_package",
                    "all_subject_masters_confirmed": all_confirmed,
                },
            ),
        )

    def stop_image_job(self, run_id: str, job_id: str) -> Result:
        queue = self._image_generation_queue()
        job = queue.get_job(job_id)
        if job is None or str(job.get("run_id") or "") != run_id:
            return Result.failure("image_job.not_found", "The image job was not found in this batch.")
        stop_reason = "用户在工具台手动停止生图"
        queue.stop(job_id, reason=stop_reason, stopped_by="workbench_user")
        image_job = self._image_job_payload(job_id)
        event = self.repo.append_run_event(
            run_id,
            "image_job.stopped",
            "Image generation was stopped by the user.",
            {"image_job_id": job_id, "stop_reason": stop_reason},
        )
        return Result.success(
            "image_job.stopped",
            "Image generation was stopped.",
            self._response_payload(self.repo.load_run(run_id), event, {"image_job": image_job}),
        )

    def resume_image_job(self, run_id: str, job_id: str) -> Result:
        queue = self._image_generation_queue()
        job = queue.get_job(job_id)
        if job is None or str(job.get("run_id") or "") != run_id:
            return Result.failure("image_job.not_found", "The image job was not found in this batch.")
        queue.resume(job_id)
        image_job = self._image_job_payload(job_id)
        event = self.repo.append_run_event(
            run_id,
            "image_job.resumed",
            "Image generation was resumed and returned to the shared worker queue.",
            {"image_job_id": job_id},
        )
        return Result.success(
            "image_job.resumed",
            "Image generation was resumed.",
            self._response_payload(self.repo.load_run(run_id), event, {"image_job": image_job}),
        )

    def approve_image_job(self, run_id: str, job_id: str) -> Result:
        queue = self._image_generation_queue()
        job = queue.get_job(job_id)
        if job is None or str(job.get("run_id") or "") != run_id:
            return Result.failure("image_job.not_found", "The image job was not found in this batch.")
        try:
            queue.approve_review(job_id)
        except ValueError as error:
            return Result.failure(
                "image_job.approval_invalid",
                f"图片确认失败：{error}",
                errors=[str(error)],
                data={"run_id": run_id, "image_job_id": job_id},
            )
        image_job = self._image_job_payload(job_id)
        event = self.repo.append_run_event(
            run_id,
            "image_job.approved",
            "The user approved all eight frozen images for the upload image gate.",
            {"image_job_id": job_id, "accepted_image_count": 8},
        )
        return Result.success(
            "image_job.approved",
            "All eight images were approved.",
            self._response_payload(
                self.repo.load_run(run_id), event, {"image_job": image_job}
            ),
        )

    def request_image_repairs(
        self,
        run_id: str,
        job_id: str,
        repairs: list[dict[str, Any]],
    ) -> Result:
        queue = self._image_generation_queue()
        job = queue.get_job(job_id)
        if job is None or str(job.get("run_id") or "") != run_id:
            return Result.failure(
                "image_job.not_found",
                "The image job was not found in this batch.",
            )
        try:
            requested = queue.request_repairs(job_id, repairs)
        except ImageRepairRequestError as error:
            return Result.failure(
                error.code,
                str(error),
                data={"run_id": run_id, "image_job_id": job_id},
            )
        image_job = self._image_job_payload(job_id)
        feedback = [
            {
                "slot_id": str(slot["slot_id"]),
                "issue_code": str(slot["review_issue_code"]),
                "note": str(slot["review_note"] or ""),
                "requested_at": slot["review_requested_at"],
            }
            for slot in requested["slots"]
        ]
        event = self.repo.append_run_event(
            run_id,
            "image_job.repair_requested",
            "Selected image slots were returned to the repair queue.",
            {
                "image_job_id": job_id,
                "slot_ids": [item["slot_id"] for item in feedback],
                "repairs": feedback,
            },
        )
        return Result.success(
            "image_job.repair_requested",
            "Selected image slots were queued for repair.",
            self._response_payload(
                self.repo.load_run(run_id),
                event,
                {"image_job": image_job, "repairs": feedback},
            ),
        )

    def image_slot_asset(self, run_id: str, job_id: str, slot_id: str) -> Result:
        queue = self._image_generation_queue()
        job = queue.get_job(job_id)
        if job is None or str(job.get("run_id") or "") != run_id:
            return Result.failure("image_asset.job_not_found", "The image job was not found in this batch.")
        slot = next(
            (item for item in queue.snapshot(job_id)["slots"] if str(item.get("slot_id") or "") == slot_id),
            None,
        )
        if slot is None or not str(slot.get("accepted_path") or ""):
            return Result.failure("image_asset.not_ready", "The requested image slot has no accepted output.")
        asset_path = Path(str(slot["accepted_path"])).resolve()
        runtime_root = self.repo.context.runtime_root.resolve()
        try:
            asset_path.relative_to(runtime_root)
        except ValueError:
            return Result.failure("image_asset.path_invalid", "The image output is outside the runtime directory.")
        if not asset_path.is_file():
            return Result.failure("image_asset.file_missing", "The accepted image output is missing from disk.")
        content_types = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }
        content_type = content_types.get(asset_path.suffix.lower())
        if content_type is None:
            return Result.failure("image_asset.type_invalid", "The accepted output is not a supported image type.")
        return Result.success(
            "image_asset.ready",
            "Accepted image output is ready.",
            {"path": str(asset_path), "content_type": content_type},
        )

    def run_until_blocked(
        self,
        run_id: str,
        max_steps: int = 20,
        should_stop: Callable[[], bool] | None = None,
    ) -> Result:
        if max_steps <= 0:
            return Result.failure("autopilot.invalid_max_steps", "max_steps must be greater than zero.")
        start_event = self.repo.append_run_event(
            run_id,
            "autopilot.started",
            "Autopilot started and will run until the next blocking gate.",
            {"max_steps": max_steps},
        )
        history: list[dict[str, Any]] = [
            {"ok": True, "code": start_event.event_type, "message": start_event.message}
        ]
        for _step in range(max_steps):
            if should_stop is not None and should_stop():
                return self._autopilot_blocked(
                    run_id,
                    "user_stopped",
                    "Autopilot stopped before the next state transition.",
                    history,
                )
            run = self.recover_browser_task_state(run_id)
            run = self._recover_pending_supplier_replacement(run)
            state = WorkbenchState(run["status"])
            if state == WorkbenchState.CREATED:
                credentials = self.credential_service.status()
                action = WorkbenchAction.START_DEDUPE if credentials.data.get("configured") else WorkbenchAction.CHECK_CREDENTIALS
                result = self.dispatch(run_id, action.value)
                history.append(self._history_item(result))
                if result.code == "workbench.store_binding_required":
                    return self._autopilot_blocked(
                        run_id,
                        "store_binding_required",
                        "Store authorization binding is required before continuing.",
                        history,
                    )
                if not result.ok:
                    return self._autopilot_blocked(run_id, result.code, result.message, history)
                continue

            if state == WorkbenchState.NEEDS_CREDENTIALS:
                credentials = self.credential_service.status()
                if not credentials.data.get("configured"):
                    return self._autopilot_blocked(
                        run_id,
                        "store_binding_required",
                        "Store authorization binding is required before continuing.",
                        history,
                    )
                result = self.dispatch(run_id, WorkbenchAction.SAVE_CREDENTIALS.value)
                history.append(self._history_item(result))
                if not result.ok:
                    return self._autopilot_blocked(run_id, result.code, result.message, history)
                continue

            if state == WorkbenchState.DEDUPING_STORE:
                result = self.dispatch(run_id, WorkbenchAction.START_DEDUPE.value)
                history.append(self._history_item(result))
                if not result.ok:
                    return self._autopilot_blocked(run_id, result.code, result.message, history)
                continue

            if state == WorkbenchState.STORE_DEDUPED:
                result = self.dispatch(run_id, WorkbenchAction.SELECT_SEEDS.value)
                history.append(self._history_item(result))
                if not result.ok:
                    return self._autopilot_blocked(run_id, result.code, result.message, history)
                continue

            if state == WorkbenchState.SEED_SELECTED:
                seeds = self._safe_load_sampled_seeds(run_id)
                if not seeds:
                    return self._autopilot_blocked(run_id, "sampled_seeds_missing", "No sampled seeds exist for this batch.", history)
                if not all(seed_has_generated_ozon_query(seed) for seed in seeds):
                    result = self.dispatch(run_id, WorkbenchAction.GENERATE_OZON_QUERIES.value)
                    history.append(self._history_item(result))
                    if result.code == "workbench.queries_need_generation":
                        return self._autopilot_blocked(
                            run_id,
                            "query_generation_required",
                            "Some sampled seeds need safe Russian-first Ozon query terms.",
                            history,
                        )
                    if not result.ok:
                        return self._autopilot_blocked(run_id, result.code, result.message, history)
                    continue
                if not run.get("ozon_collection_contract_ready"):
                    result = self.dispatch(run_id, WorkbenchAction.START_OZON_COLLECTION.value)
                    history.append(self._history_item(result))
                    if not result.ok:
                        return self._autopilot_blocked(run_id, result.code, result.message, history)
                    continue
                return self._autopilot_blocked(
                    run_id,
                    "collection_worker_required",
                    "Ozon collection contract is ready; an approved browser worker must lock the final products.",
                    history,
                )

            if state == WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTING:
                return self._autopilot_blocked(
                    run_id,
                    "attribute_template_worker_required",
                    "Ozon attribute template contract is ready; an approved browser worker must collect and ingest the template.",
                    history,
                )

            if state == WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTED:
                result = self.dispatch(run_id, WorkbenchAction.OPEN_SUPPLIER_REVIEW.value)
                history.append(self._history_item(result))
                if not result.ok:
                    return self._autopilot_blocked(run_id, result.code, result.message, history)
                continue

            if state == WorkbenchState.OZON_COLLECTING:
                return self._autopilot_blocked(
                    run_id,
                    "collection_worker_required",
                    "Ozon collection contract is ready; an approved browser worker must collect and ingest the products.",
                    history,
                )

            if state == WorkbenchState.OZON_COLLECTED:
                result = self.dispatch(
                    run_id,
                    WorkbenchAction.START_ATTRIBUTE_TEMPLATE_COLLECTION.value,
                )
                history.append(self._history_item(result))
                if not result.ok:
                    return self._autopilot_blocked(
                        run_id,
                        result.code,
                        result.message,
                        history,
                    )
                continue

            if state == WorkbenchState.SUPPLIER_REVIEW:
                return self._autopilot_blocked(
                    run_id,
                    "supplier_review_required",
                    "Ozon collection is complete; user-verified 1688 product links are required.",
                    history,
                )

            if state == WorkbenchState.SUPPLIER_COLLECTING:
                return self._autopilot_blocked(
                    run_id,
                    "supplier_collection_worker_required",
                    "The direct-network 1688 supplier collection worker must collect the submitted links.",
                    history,
                )

            if state == WorkbenchState.SUPPLIER_COLLECTED:
                selection_status = self._supplier_sku_selection_status(run_id)
                if not selection_status["complete"]:
                    return self._autopilot_blocked(
                        run_id,
                        "supplier_sku_selection_required",
                        "Supplier collection is complete; one real 1688 SKU must be locked for every product.",
                        history,
                    )
                return self._autopilot_blocked(
                    run_id,
                    "collection_review_required",
                    "Ozon and 1688 collection evidence is ready for user review.",
                    history,
                )

            if state == WorkbenchState.IMAGE_PROCESSING:
                return self._autopilot_blocked(
                    run_id,
                    "upload_preparation_required",
                    "Supplier subject evidence is locked; complete fields and pricing, then submit ready products individually.",
                    history,
                )

            if state in {
                WorkbenchState.NEEDS_MANUAL_REVIEW,
                WorkbenchState.NEEDS_SLIDER,
                WorkbenchState.FAILED_RETRYABLE,
                WorkbenchState.FAILED_BLOCKED,
                WorkbenchState.DRAFT_READY,
                WorkbenchState.PUBLISH_WAITING_CONFIRMATION,
            }:
                return self._autopilot_blocked(
                    run_id,
                    state.value,
                    "Autopilot stopped at a state that requires manual handling or a later implementation gate.",
                    history,
                )

            if state == WorkbenchState.DONE:
                return self._autopilot_blocked(run_id, "done", "Batch is already done.", history)

            return self._autopilot_blocked(
                run_id,
                f"not_implemented:{state.value}",
                "Autopilot reached a state whose automation is not implemented yet.",
                history,
            )

        return self._autopilot_blocked(
            run_id,
            "step_limit_reached",
            "Autopilot stopped because the step limit was reached.",
            history,
        )

    def dispatch(self, run_id: str, action: str) -> Result:
        run = self.repo.load_run(run_id)
        current = WorkbenchState(run["status"])
        try:
            parsed_action = WorkbenchAction(action)
        except ValueError:
            event = self.repo.append_run_event(
                run_id,
                "workbench.action_rejected",
                "Unknown workbench action rejected.",
                {"action": action, "status": current.value},
            )
            return Result.failure(
                "workbench.unknown_action",
                "Unknown workbench action.",
                data={"run_id": run_id, "status": current.value, "gates": self._gate_status(), "last_event": event.to_dict()},
            )
        if parsed_action in _INTERNAL_LIFECYCLE_ACTIONS:
            event = self.repo.append_run_event(
                run_id,
                "workbench.action_rejected",
                "Internal lifecycle transitions cannot be invoked as operator actions.",
                {"action": parsed_action.value, "status": current.value},
            )
            return Result.failure(
                "workbench.internal_action_forbidden",
                "This lifecycle transition is applied only after its evidence has been validated.",
                data={
                    "run_id": run_id,
                    "status": current.value,
                    "action": parsed_action.value,
                    "allowed_actions": self._allowed_action_values(run),
                    "gates": self._gate_status(),
                    "last_event": event.to_dict(),
                },
            )
        if parsed_action == WorkbenchAction.CHECK_CREDENTIALS:
            return self._check_credentials(run)
        if parsed_action == WorkbenchAction.SAVE_CREDENTIALS:
            return self._recheck_saved_credentials(run)
        if parsed_action == WorkbenchAction.START_DEDUPE:
            return self._start_store_dedupe(run)
        if parsed_action == WorkbenchAction.SELECT_SEEDS:
            return self._select_seeds(run)
        if parsed_action == WorkbenchAction.GENERATE_OZON_QUERIES:
            return self._generate_ozon_queries(run)
        if parsed_action == WorkbenchAction.START_ATTRIBUTE_TEMPLATE_COLLECTION:
            return self._prepare_attribute_template_contract(run)
        if parsed_action == WorkbenchAction.MARK_ATTRIBUTE_TEMPLATE_COLLECTED:
            return self._mark_attribute_template_collected(run)
        if parsed_action == WorkbenchAction.START_OZON_COLLECTION:
            return self._prepare_ozon_collection_contract(run)
        if parsed_action == WorkbenchAction.START_IMAGE_PROCESSING and current == WorkbenchState.SUPPLIER_COLLECTED:
            selection_status = self._supplier_sku_selection_status(run_id)
            if not selection_status["complete"]:
                code = "supplier_sku_selection.invalid" if selection_status["invalid"] else "supplier_sku_selection.required"
                event = self.repo.append_run_event(
                    run_id,
                    code,
                    "Image processing is locked until every real supplier SKU selection receipt is valid.",
                    selection_status,
                )
                return Result.failure(
                    code,
                    "Image processing is locked until every real supplier SKU selection receipt is valid.",
                    data=self._response_payload(run, event, {"selection_status": selection_status}),
                )
            review_result = self.supplier_review(run_id)
            if not review_result.ok or not review_result.data.get("can_approve"):
                event = self.repo.append_run_event(
                    run_id,
                    "collection_review.approval_rejected",
                    "Collection review cannot be approved while required evidence is missing.",
                    {"status": current.value},
                )
                return Result.failure(
                    "collection_review.incomplete",
                    "Required Ozon or 1688 collection evidence is missing.",
                    data={
                        "run_id": run_id,
                        "status": current.value,
                        "review": review_result.data if review_result.ok else {},
                        "last_event": event.to_dict(),
                    },
                )
            event = self.repo.append_run_event(
                run_id,
                "subject_master.required",
                "Image processing starts automatically only after subject evidence is confirmed for every exact supplier SKU.",
                {"status": current.value},
            )
            return Result.failure(
                "subject_master.required",
                "Confirm one or more exact supplier subject evidence images for every locked supplier SKU.",
                data=self._response_payload(run, event),
            )
        if parsed_action == WorkbenchAction.REQUEST_PUBLISH and run.get("publish_locked", True):
            event = self.repo.append_run_event(
                run_id,
                "publish.locked",
                "Publish request rejected because v0.1 publish lock is enabled.",
                {"status": current.value, "action": parsed_action.value},
            )
            return Result.failure(
                "publish.locked",
                "Publish is locked in v0.1. Build and review drafts first; do not submit to Ozon.",
                data={"run_id": run_id, "status": current.value, "gates": self._gate_status(), "last_event": event.to_dict()},
            )
        try:
            next_state = transition_workbench_state(current, parsed_action)
        except ValueError as exc:
            event = self.repo.append_run_event(
                run_id,
                "workbench.action_rejected",
                "Workbench action rejected by state machine.",
                {"status": current.value, "action": parsed_action.value},
            )
            return Result.failure(
                "workbench.invalid_action",
                str(exc),
                data={
                    "run_id": run_id,
                    "status": current.value,
                    "action": parsed_action.value,
                    "allowed_actions": self._allowed_action_values(run),
                    "gates": self._gate_status(),
                    "last_event": event.to_dict(),
                },
            )
        run["status"] = next_state.value
        self.repo.save_run(run)
        event = self.repo.append_run_event(
            run_id,
            "workbench.action_applied",
            "Workbench action applied by state machine.",
            {"from_status": current.value, "to_status": next_state.value, "action": parsed_action.value},
        )
        return Result.success(
            "workbench.action_applied",
            "Workbench action applied.",
            {
                "run": run,
                "allowed_actions": self._allowed_action_values(run),
                "gates": self._gate_status(),
                "last_event": event.to_dict(),
            },
        )

    def _allowed_action_values(self, run: dict) -> list[str]:
        state = WorkbenchState(run["status"])
        actions = allowed_workbench_actions(state, publish_locked=run.get("publish_locked", True))
        return [
            action.value
            for action in actions
            if action not in _INTERNAL_LIFECYCLE_ACTIONS
        ]

    def _recover_pending_supplier_replacement(self, run: dict[str, Any]) -> dict[str, Any]:
        pending_seed_ids = [
            str(seed_id).strip()
            for seed_id in run.get("replacement_pending_seed_ids") or []
            if str(seed_id).strip()
        ]
        current = WorkbenchState(run["status"])
        if not pending_seed_ids or current not in _REPLACEMENT_RECOVERY_STATES:
            return run

        if run.get("attribute_template_collected"):
            target = WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTED
        elif run.get("attribute_template_contract_ready"):
            target = WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTING
        elif run.get("ozon_collected"):
            target = WorkbenchState.OZON_COLLECTED
        elif run.get("ozon_collection_contract_ready"):
            target = WorkbenchState.OZON_COLLECTING
        else:
            target = WorkbenchState.SEED_SELECTED

        run["status"] = target.value
        run["ozon_collected"] = target in {
            WorkbenchState.OZON_COLLECTED,
            WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTING,
            WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTED,
        }
        run["browser_task_cancelled"] = False
        run.pop("browser_task_cancelled_at", None)
        run.pop("browser_task_cancel_reason", None)
        self.repo.save_run(run)
        if target == WorkbenchState.OZON_COLLECTING:
            try:
                self._reconcile_replacement_ozon_checkpoint(run)
            except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
                pass
        self.repo.append_run_event(
            run["run_id"],
            "supplier_review.replacement_recovery_started",
            "A pending replacement product was restored to its verified collection stage.",
            {
                "from_status": current.value,
                "to_status": target.value,
                "replacement_pending_seed_ids": pending_seed_ids,
            },
        )
        return run

    def _reconcile_replacement_ozon_checkpoint(self, run: dict[str, Any]) -> None:
        pending_seed_ids = {
            str(seed_id).strip()
            for seed_id in run.get("replacement_pending_seed_ids") or []
            if str(seed_id).strip()
        }
        draft_path = self.repo.run_dir(run["run_id"]) / "ozon_collection_draft.json"
        if not pending_seed_ids or not draft_path.exists():
            return

        contract = self.repo.load_ozon_collection_contract(run["run_id"])
        payload = contract.get("payload")
        seeds = payload.get("seeds") if isinstance(payload, dict) else None
        if not isinstance(seeds, list):
            return
        contract_seed_ids = {
            str(seed.get("seed_id") or "").strip()
            for seed in seeds
            if isinstance(seed, dict) and str(seed.get("seed_id") or "").strip()
        }
        draft = self.repo.load_ozon_collection_draft(run["run_id"])
        candidates = draft.get("ozon_candidates")
        if not isinstance(candidates, list):
            return
        retained = [
            candidate
            for candidate in candidates
            if isinstance(candidate, dict)
            and str(candidate.get("seed_id") or "").strip() in contract_seed_ids
        ]
        if len(retained) == len(candidates):
            return
        if retained:
            draft["ozon_candidates"] = retained
            draft["updated_at"] = utc_now_iso()
            self.repo.save_ozon_collection_draft(run["run_id"], draft)
        else:
            draft_path.unlink(missing_ok=True)
        self.repo.append_run_event(
            run["run_id"],
            "ozon_collection.replacement_checkpoint_reconciled",
            "Stale checkpoint items from the rejected product were removed before replacement collection resumed.",
            {
                "removed_count": len(candidates) - len(retained),
                "retained_count": len(retained),
                "replacement_pending_seed_ids": sorted(pending_seed_ids),
            },
        )

    def _ensure_candidate_slots(
        self,
        run: dict[str, Any],
        seeds: list[SeedProduct],
    ) -> dict[str, dict[str, Any]]:
        existing = [
            dict(item)
            for item in run.get("candidate_slots", [])
            if isinstance(item, dict) and item.get("seed_id")
        ]
        by_seed = {str(item["seed_id"]): item for item in existing}
        used_slot_ids = {
            str(item.get("slot_id") or "")
            for item in existing
            if str(item.get("slot_id") or "").strip()
        }
        ordered: list[dict[str, Any]] = []
        for index, seed in enumerate(seeds, start=1):
            item = by_seed.get(seed.seed_id)
            if item is None:
                slot_id = f"slot-{index:04d}"
                suffix = index
                while slot_id in used_slot_ids:
                    suffix += 1
                    slot_id = f"slot-{suffix:04d}"
                used_slot_ids.add(slot_id)
                item = {
                    "slot_id": slot_id,
                    "seed_id": seed.seed_id,
                    "candidate_revision": 1,
                    "ozon_product_id": None,
                }
            item["slot_id"] = str(item.get("slot_id") or f"slot-{index:04d}")
            item["seed_id"] = seed.seed_id
            item["candidate_revision"] = max(
                1,
                int(item.get("candidate_revision") or 1),
            )
            ordered.append(item)
        run["candidate_slots"] = ordered
        return {str(item["seed_id"]): item for item in ordered}

    def _replace_candidate_slot(
        self,
        run: dict[str, Any],
        *,
        rejected_seed_id: str,
        replacement_seed_id: str,
        rejected_ozon_product_id: str,
        reason: str,
    ) -> dict[str, Any]:
        slots = [
            dict(item)
            for item in run.get("candidate_slots", [])
            if isinstance(item, dict)
        ]
        slot = next(
            (
                item
                for item in slots
                if str(item.get("seed_id") or "") == rejected_seed_id
            ),
            None,
        )
        if slot is None:
            slot = {
                "slot_id": f"slot-{len(slots) + 1:04d}",
                "seed_id": rejected_seed_id,
                "candidate_revision": 1,
                "ozon_product_id": rejected_ozon_product_id,
            }
            slots.append(slot)
        previous_revision = int(slot.get("candidate_revision") or 1)
        history = list(run.get("candidate_revision_history") or [])
        history.append(
            {
                "slot_id": str(slot.get("slot_id") or ""),
                "candidate_revision": previous_revision,
                "seed_id": rejected_seed_id,
                "ozon_product_id": rejected_ozon_product_id,
                "reason": reason,
                "retired_at": utc_now_iso(),
            }
        )
        slot["seed_id"] = replacement_seed_id
        slot["candidate_revision"] = previous_revision + 1
        slot["ozon_product_id"] = None
        slot["updated_at"] = utc_now_iso()
        run["candidate_slots"] = slots
        run["candidate_revision_history"] = history
        return slot

    def _stamp_ozon_candidate_identities(
        self,
        run: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> list[str]:
        slots = self._ensure_candidate_slots(
            run,
            self._safe_load_sampled_seeds(run["run_id"]),
        )
        errors: list[str] = []
        for candidate in candidates:
            seed_id = str(candidate.get("seed_id") or "")
            slot = slots.get(seed_id)
            if slot is None:
                errors.append(f"candidate seed {seed_id!r} has no active slot")
                continue
            expected_slot = str(slot["slot_id"])
            expected_revision = int(slot["candidate_revision"])
            provided_slot = str(candidate.get("slot_id") or "").strip()
            provided_revision = candidate.get("candidate_revision")
            if provided_slot and provided_slot != expected_slot:
                errors.append(
                    f"candidate {seed_id} slot_id {provided_slot!r} does not match {expected_slot!r}"
                )
            if provided_revision not in (None, ""):
                try:
                    parsed_revision = int(provided_revision)
                except (TypeError, ValueError):
                    errors.append(
                        f"candidate {seed_id} revision {provided_revision!r} is invalid"
                    )
                else:
                    if parsed_revision != expected_revision:
                        errors.append(
                            f"candidate {seed_id} revision {provided_revision!r} does not match {expected_revision}"
                        )
            candidate["slot_id"] = expected_slot
            candidate["candidate_revision"] = expected_revision
            slot["ozon_product_id"] = str(candidate.get("ozon_product_id") or "") or None
            slot["updated_at"] = utc_now_iso()
        return errors

    def _select_seeds(self, run: dict) -> Result:
        if not self._action_is_allowed(run, WorkbenchAction.SELECT_SEEDS):
            return self._reject_invalid_action(run, WorkbenchAction.SELECT_SEEDS)
        dedupe_status = self.repo.existing_store_dedupe_status()
        if not dedupe_status["ready"]:
            event = self.repo.append_run_event(
                run["run_id"],
                "seed_sampling.blocked_missing_dedupe",
                "Seed sampling was blocked because existing-store dedupe is not ready.",
                dedupe_status,
            )
            return Result.failure(
                "workbench.dedupe_missing",
                "Existing-store dedupe must be ready before seed sampling.",
                data=self._response_payload(run, event),
            )
        target_count = int(run.get("target_count", 0))
        used_seed_ids = self.repo.load_used_seed_ids()
        blacklisted_seed_ids = self.repo.load_blacklisted_seed_ids()
        used_seed_identity_keys = self.repo.load_used_seed_identity_keys()
        blacklisted_seed_identity_keys = self.repo.load_blacklisted_seed_identity_keys()
        existing_products = self.repo.load_existing_products()
        eligible: list[SeedProduct] = []
        blocked: list[dict[str, Any]] = []
        for seed in self.repo.load_active_seeds():
            if seed.seed_id in blacklisted_seed_ids:
                blocked.append({"seed_id": seed.seed_id, "reason": "blacklisted"})
                continue
            if self.repo.seed_identity_key(seed) in blacklisted_seed_identity_keys:
                blocked.append({"seed_id": seed.seed_id, "reason": "blacklisted identity"})
                continue
            if seed.seed_id in used_seed_ids:
                blocked.append({"seed_id": seed.seed_id, "reason": "already used"})
                continue
            if self.repo.seed_identity_key(seed) in used_seed_identity_keys:
                blocked.append({"seed_id": seed.seed_id, "reason": "already used identity"})
                continue
            decision = decide_seed_existing_product_dedupe(seed, existing_products)
            if decision.kind.value != "clear":
                blocked.append({"seed_id": seed.seed_id, "reason": decision.reason})
                continue
            eligible.append(seed)
        if len(eligible) < target_count:
            event = self.repo.append_run_event(
                run["run_id"],
                "seed_sampling.insufficient_seeds",
                "Not enough eligible seeds after existing-store dedupe filtering.",
                {"eligible_seed_count": len(eligible), "target_count": target_count, "blocked_seed_count": len(blocked)},
            )
            return Result.failure(
                "workbench.insufficient_seeds",
                "Not enough eligible seeds after existing-store dedupe filtering.",
                data=self._response_payload(run, event),
            )
        random_seed = random.SystemRandom().randint(1, 2**31 - 1)
        sampled = self.repo.sample_seeds(eligible, target_count, random_seed)
        self.repo.save_sampled_seeds(run["run_id"], sampled)
        self.repo.append_used_seeds(run["run_id"], sampled, "workbench_sampled")
        current = WorkbenchState(run["status"])
        run["status"] = transition_workbench_state(current, WorkbenchAction.SELECT_SEEDS).value
        run["random_seed"] = random_seed
        run["sampled_seed_ids"] = [seed.seed_id for seed in sampled]
        self._ensure_candidate_slots(run, sampled)
        run["blocked_seed_count"] = len(blocked)
        self._update_query_summary(run, sampled)
        self.repo.save_run(run)
        event = self.repo.append_run_event(
            run["run_id"],
            "seed_sampling.selected",
            "Eligible seeds were sampled for this workbench batch.",
            {
                "sampled_seed_ids": run["sampled_seed_ids"],
                "random_seed": random_seed,
                "blocked_seed_count": len(blocked),
            },
        )
        return Result.success(
            "workbench.seeds_selected",
            "Eligible seeds were sampled for this workbench batch.",
            self._response_payload(run, event),
        )

    def _generate_ozon_queries(self, run: dict) -> Result:
        if not self._action_is_allowed(run, WorkbenchAction.GENERATE_OZON_QUERIES):
            return self._reject_invalid_action(run, WorkbenchAction.GENERATE_OZON_QUERIES)
        seeds = self._safe_load_sampled_seeds(run["run_id"])
        generated: list[SeedProduct] = []
        missing: list[dict[str, Any]] = []
        invalid: list[dict[str, Any]] = []
        primary_query_owners: dict[str, str] = {}
        for seed in seeds:
            result = self.seed_query_service.generate_for_seed(seed)
            if result.ok:
                query = SeedSearchQuery.from_dict(result.data)
                primary_query = str((query.ozon_query_terms_ru or [""])[0]).strip().casefold()
                existing_owner = primary_query_owners.get(primary_query)
                if primary_query and existing_owner and existing_owner != seed.seed_id:
                    seed.ozon_query_terms_ru = []
                    seed.query_generation_status = QueryGenerationStatus.NEEDS_QUERY_GENERATION
                    invalid.append(
                        {
                            "seed_id": seed.seed_id,
                            "code": "duplicate_primary_query",
                            "query": primary_query,
                            "conflicts_with_seed_id": existing_owner,
                        }
                    )
                    generated.append(seed)
                    continue
                if primary_query:
                    primary_query_owners[primary_query] = seed.seed_id
                updated = self.seed_query_service.apply_query_to_seed(seed, query)
                errors = validate_seed_ready_for_ozon(updated)
                if errors:
                    invalid.append({"seed_id": seed.seed_id, "errors": errors})
                generated.append(updated)
                continue
            seed.query_generation_status = QueryGenerationStatus.NEEDS_QUERY_GENERATION
            missing.append({"seed_id": seed.seed_id, "title_or_keyword": seed.title_or_keyword, "code": result.code})
            generated.append(seed)
        self.repo.save_sampled_seeds(run["run_id"], generated)
        self._update_query_summary(run, generated)
        self.repo.save_run(run)
        if missing or invalid:
            event = self.repo.append_run_event(
                run["run_id"],
                "query_generation.needs_input",
                "Some sampled seeds still need safe Russian-first Ozon query terms.",
                {"missing": missing, "invalid": invalid},
            )
            return Result.success(
                "workbench.queries_need_generation",
                "Some sampled seeds still need safe Russian-first Ozon query terms.",
                self._response_payload(run, event, {"missing_queries": missing, "invalid_queries": invalid}),
            )
        event = self.repo.append_run_event(
            run["run_id"],
            "query_generation.generated",
            "All sampled seeds have safe Russian-first Ozon query terms.",
            {"query_ready_count": len(generated)},
        )
        return Result.success(
            "workbench.queries_generated",
            "All sampled seeds have safe Russian-first Ozon query terms.",
            self._response_payload(run, event),
        )

    def _prepare_attribute_template_contract(self, run: dict) -> Result:
        if not self._action_is_allowed(run, WorkbenchAction.START_ATTRIBUTE_TEMPLATE_COLLECTION):
            return self._reject_invalid_action(run, WorkbenchAction.START_ATTRIBUTE_TEMPLATE_COLLECTION)
        seeds = self._safe_load_sampled_seeds(run["run_id"])
        self._ensure_candidate_slots(run, seeds)
        self.repo.save_run(run)
        try:
            ozon_result = self.repo.load_ozon_collection_result(run["run_id"])
        except (FileNotFoundError, json.JSONDecodeError):
            ozon_result = {}
        locked_errors = validate_ozon_collection_result(
            ozon_result,
            [seed.seed_id for seed in seeds],
        )
        if locked_errors:
            event = self.repo.append_run_event(
                run["run_id"],
                "attribute_template.blocked_missing_final_ozon_product",
                "Attribute template collection was blocked until every final Ozon product is locked.",
                {"errors": locked_errors},
            )
            return Result.failure(
                "workbench.final_ozon_product_required",
                "Every candidate slot must lock its final Ozon product before category template collection.",
                errors=locked_errors,
                data=self._response_payload(run, event),
            )
        missing = [
            {"seed_id": seed.seed_id, "title_or_keyword": seed.title_or_keyword}
            for seed in seeds
            if not seed_has_generated_ozon_query(seed)
        ]
        if missing:
            event = self.repo.append_run_event(
                run["run_id"],
                "attribute_template.blocked_missing_queries",
                "Ozon attribute template contract was blocked because sampled seeds still need generated query terms.",
                {"missing": missing},
            )
            return Result.failure(
                "workbench.missing_ozon_queries",
                "Generated Russian-first Ozon query terms are required before attribute template collection.",
                data=self._response_payload(run, event, {"missing_queries": missing}),
            )
        contract_result = self.collection_contract_service.build_attribute_template_contract(run["run_id"])
        if not contract_result.ok:
            event = self.repo.append_run_event(
                run["run_id"],
                "attribute_template.contract_failed",
                "Ozon attribute template contract generation failed.",
                {"code": contract_result.code, "errors": contract_result.errors, "data": contract_result.data},
            )
            return Result.failure(
                "workbench.attribute_template_contract_failed",
                "Ozon attribute template contract generation failed.",
                errors=contract_result.errors,
                data=self._response_payload(run, event, {"contract": contract_result.to_dict()}),
            )
        current = WorkbenchState(run["status"])
        run["status"] = transition_workbench_state(current, WorkbenchAction.START_ATTRIBUTE_TEMPLATE_COLLECTION).value
        run["attribute_template_contract_ready"] = True
        contract_path = self.repo.save_attribute_template_contract(run["run_id"], contract_result.data)
        run["attribute_template_contract_path"] = str(contract_path)
        self.repo.save_run(run)
        event = self.repo.append_run_event(
            run["run_id"],
            "attribute_template.contract_ready",
            "Ozon attribute template task contract is ready for an approved browser worker.",
            {"contract_code": contract_result.code, "contract_path": str(contract_path)},
        )
        return Result.success(
            "workbench.attribute_template_contract_ready",
            "Ozon attribute template task contract is ready. No browser collection was started by the workbench.",
            self._response_payload(run, event, {"contract": contract_result.to_dict()}),
        )

    def _mark_attribute_template_collected(self, run: dict) -> Result:
        if not self._action_is_allowed(run, WorkbenchAction.MARK_ATTRIBUTE_TEMPLATE_COLLECTED):
            return self._reject_invalid_action(run, WorkbenchAction.MARK_ATTRIBUTE_TEMPLATE_COLLECTED)
        current = WorkbenchState(run["status"])
        run["status"] = transition_workbench_state(current, WorkbenchAction.MARK_ATTRIBUTE_TEMPLATE_COLLECTED).value
        run["attribute_template_collected"] = True
        self.repo.save_run(run)
        event = self.repo.append_run_event(
            run["run_id"],
            "attribute_template.collected",
            "Ozon attribute template evidence was marked collected.",
            {"attribute_template_collected": True},
        )
        return Result.success(
            "workbench.attribute_template_collected",
            "Ozon attribute template evidence was marked collected.",
            self._response_payload(run, event),
        )

    def _prepare_ozon_collection_contract(self, run: dict) -> Result:
        if not self._action_is_allowed(run, WorkbenchAction.START_OZON_COLLECTION):
            return self._reject_invalid_action(run, WorkbenchAction.START_OZON_COLLECTION)
        seeds = self._safe_load_sampled_seeds(run["run_id"])
        self._ensure_candidate_slots(run, seeds)
        self.repo.save_run(run)
        missing = [
            {"seed_id": seed.seed_id, "title_or_keyword": seed.title_or_keyword}
            for seed in seeds
            if not seed_has_generated_ozon_query(seed)
        ]
        if missing:
            event = self.repo.append_run_event(
                run["run_id"],
                "ozon_collection.blocked_missing_queries",
                "Ozon collection contract was blocked because sampled seeds still need generated query terms.",
                {"missing": missing},
            )
            return Result.failure(
                "workbench.missing_ozon_queries",
                "Generated Russian-first Ozon query terms are required before Ozon collection.",
                data=self._response_payload(run, event, {"missing_queries": missing}),
            )
        contract_result = self.collection_contract_service.build_ozon_collection_contract(run["run_id"])
        if not contract_result.ok:
            event = self.repo.append_run_event(
                run["run_id"],
                "ozon_collection.contract_failed",
                "Ozon collection contract generation failed.",
                {"code": contract_result.code, "errors": contract_result.errors, "data": contract_result.data},
            )
            return Result.failure(
                "workbench.ozon_contract_failed",
                "Ozon collection contract generation failed.",
                errors=contract_result.errors,
                data=self._response_payload(run, event, {"contract": contract_result.to_dict()}),
            )
        current = WorkbenchState(run["status"])
        run["status"] = transition_workbench_state(current, WorkbenchAction.START_OZON_COLLECTION).value
        run["ozon_collection_contract_ready"] = True
        contract_path = self.repo.save_ozon_collection_contract(run["run_id"], contract_result.data)
        run["ozon_collection_contract_path"] = str(contract_path)
        self.repo.save_run(run)
        event = self.repo.append_run_event(
            run["run_id"],
            "ozon_collection.contract_ready",
            "Ozon collection task contract is ready for an approved browser worker.",
            {"contract_code": contract_result.code, "contract_path": str(contract_path)},
        )
        return Result.success(
            "workbench.ozon_contract_ready",
            "Ozon collection task contract is ready. No browser collection was started by the workbench.",
            self._response_payload(run, event, {"contract": contract_result.to_dict()}),
        )

    def _check_credentials(self, run: dict) -> Result:
        if not self._action_is_allowed(run, WorkbenchAction.CHECK_CREDENTIALS):
            return self._reject_invalid_action(run, WorkbenchAction.CHECK_CREDENTIALS)
        credential_result = self.credential_service.status()
        if credential_result.data.get("configured"):
            event = self.repo.append_run_event(
                run["run_id"],
                "credentials.configured",
                "Seller credentials are configured.",
                {"client_id": credential_result.data.get("client_id")},
            )
            return Result.success(
                "workbench.credentials_configured",
                "Seller credentials are configured. Store dedupe can be started.",
                self._response_payload(run, event),
            )
        next_state = transition_workbench_state(WorkbenchState(run["status"]), WorkbenchAction.CHECK_CREDENTIALS)
        run["status"] = next_state.value
        self.repo.save_run(run)
        event = self.repo.append_run_event(
            run["run_id"],
            "store_binding.required",
            "Store authorization binding is required before continuing.",
            {"credentials_configured": False},
        )
        return Result.success(
            "workbench.store_binding_required",
            "Store authorization binding is required before continuing.",
            self._response_payload(run, event),
        )

    def _recheck_saved_credentials(self, run: dict) -> Result:
        if not self._action_is_allowed(run, WorkbenchAction.SAVE_CREDENTIALS):
            return self._reject_invalid_action(run, WorkbenchAction.SAVE_CREDENTIALS)
        credential_result = self.credential_service.status()
        if not credential_result.data.get("configured"):
            event = self.repo.append_run_event(
                run["run_id"],
                "store_binding.still_required",
                "Store authorization binding is still required before continuing.",
                {"credentials_configured": False},
            )
            return Result.success(
                "workbench.store_binding_still_required",
                "Store authorization binding is still required before continuing.",
                self._response_payload(run, event),
            )
        next_state = transition_workbench_state(WorkbenchState(run["status"]), WorkbenchAction.SAVE_CREDENTIALS)
        run["status"] = next_state.value
        self.repo.save_run(run)
        event = self.repo.append_run_event(
            run["run_id"],
            "credentials.configured",
            "Seller credentials are configured; batch returned to the first gate.",
            {"client_id": credential_result.data.get("client_id")},
        )
        return Result.success(
            "workbench.credentials_configured",
            "Seller credentials are configured. Store dedupe can be started.",
            self._response_payload(run, event),
        )

    def _start_store_dedupe(self, run: dict) -> Result:
        if not self._action_is_allowed(run, WorkbenchAction.START_DEDUPE):
            return self._reject_invalid_action(run, WorkbenchAction.START_DEDUPE)
        credential_result = self.credential_service.status()
        if not credential_result.data.get("configured"):
            next_state = transition_workbench_state(WorkbenchState(run["status"]), WorkbenchAction.CHECK_CREDENTIALS)
            run["status"] = next_state.value
            self.repo.save_run(run)
            event = self.repo.append_run_event(
                run["run_id"],
                "dedupe.blocked_missing_store_binding",
                "Store dedupe was blocked because store authorization is missing.",
                {"credentials_configured": False},
            )
            return Result.success(
                "workbench.store_binding_required",
                "Store authorization binding is required before refreshing existing-store dedupe.",
                self._response_payload(run, event),
            )
        run["status"] = transition_workbench_state(WorkbenchState(run["status"]), WorkbenchAction.START_DEDUPE).value
        self.repo.save_run(run)
        self.repo.append_run_event(
            run["run_id"],
            "dedupe.refresh_started",
            "Existing-store dedupe refresh started.",
            {"client_id": credential_result.data.get("client_id")},
        )
        refresh_result = self.seller_history_service.refresh_from_adapter()
        current = WorkbenchState(run["status"])
        if refresh_result.ok:
            run["status"] = transition_workbench_state(current, WorkbenchAction.MARK_STORE_DEDUPED).value
            self.repo.save_run(run)
            event = self.repo.append_run_event(
                run["run_id"],
                "dedupe.refreshed",
                "Existing-store dedupe refresh finished.",
                refresh_result.data,
            )
            return Result.success(
                "workbench.store_deduped",
                "Existing-store dedupe refresh finished.",
                self._response_payload(run, event, {"dedupe_refresh": refresh_result.to_dict()}),
            )
        run["status"] = transition_workbench_state(current, WorkbenchAction.MARK_FAILED_RETRYABLE).value
        self.repo.save_run(run)
        event = self.repo.append_run_event(
            run["run_id"],
            "dedupe.refresh_failed",
            "Existing-store dedupe refresh failed.",
            {"errors": refresh_result.errors, "code": refresh_result.code},
        )
        return Result.failure(
            "workbench.dedupe_refresh_failed",
            "Existing-store dedupe refresh failed.",
            errors=refresh_result.errors,
            data=self._response_payload(run, event, {"dedupe_refresh": refresh_result.to_dict()}),
        )

    def _action_is_allowed(self, run: dict, action: WorkbenchAction) -> bool:
        return action.value in self._allowed_action_values(run)

    def _reject_invalid_action(self, run: dict, action: WorkbenchAction) -> Result:
        event = self.repo.append_run_event(
            run["run_id"],
            "workbench.action_rejected",
            "Workbench action rejected by state machine.",
            {"status": run["status"], "action": action.value},
        )
        return Result.failure(
            "workbench.invalid_action",
            f"invalid workbench action: {run['status']} + {action.value}",
            data={
                "run_id": run["run_id"],
                "status": run["status"],
                "action": action.value,
                "allowed_actions": self._allowed_action_values(run),
                "gates": self._gate_status(),
                "last_event": event.to_dict(),
            },
        )

    def _response_payload(self, run: dict, event, extra: dict | None = None) -> dict:
        payload = {
            "run": run,
            "allowed_actions": self._allowed_action_values(run),
            "gates": self._gate_status(),
            "progress": self._run_progress(run),
            "last_event": event.to_dict(),
        }
        if extra:
            payload.update(extra)
        return payload

    def _autopilot_blocked(
        self,
        run_id: str,
        reason: str,
        message: str,
        history: list[dict[str, Any]],
    ) -> Result:
        run = self.repo.load_run(run_id)
        event = self.repo.append_run_event(
            run_id,
            "autopilot.blocked",
            message,
            {"blocked_reason": reason, "status": run["status"]},
        )
        return Result.success(
            "autopilot.blocked",
            message,
            self._response_payload(
                run,
                event,
                {
                    "blocked_reason": reason,
                    "action_history": history,
                },
            ),
        )

    def _history_item(self, result: Result) -> dict[str, Any]:
        run = result.data.get("run", {}) if isinstance(result.data, dict) else {}
        return {
            "ok": result.ok,
            "code": result.code,
            "message": result.message,
            "status": run.get("status"),
        }

    def _gate_status(self) -> dict:
        return {
            "credentials": self.repo.credential_status().to_dict(),
            "existing_store_dedupe": self.repo.existing_store_dedupe_status(),
        }

    def _ozon_collection_progress(self, run: dict, seeds: list[SeedProduct]) -> dict[str, Any]:
        total = len(seeds)
        expected_seed_ids = {seed.seed_id for seed in seeds}
        bridge = self.repo.load_browser_bridge_status()
        live: dict[str, Any] = {}
        if (
            str(bridge.get("run_id") or "") == str(run["run_id"])
            and bridge.get("task_type") == "ozon_collection"
        ):
            details = bridge.get("details") if isinstance(bridge.get("details"), dict) else {}
            candidate = details.get("collection_progress")
            if isinstance(candidate, dict):
                live = candidate

        events = self.repo.load_run_events(run["run_id"])
        replaced = {
            str(event.data.get("seed_id") or "")
            for event in events
            if (
                event.event_type == "browser_candidate.replaced"
                and event.data.get("task_type") == "ozon_collection"
            )
        } - {""}
        failed = ({
            str(event.data.get("seed_id") or "")
            for event in events
            if (
                event.event_type == "browser_candidate.failed"
                and event.data.get("task_type") == "ozon_collection"
            )
        } - {""}) - replaced

        durable_candidates: list[dict[str, Any]] = []
        final_path = self.repo.run_dir(run["run_id"]) / "ozon_collection_result.json"
        if final_path.exists():
            final_candidates = [
                candidate
                for candidate in self.repo.load_ozon_collection_result(
                    run["run_id"]
                ).get("ozon_candidates", [])
                if isinstance(candidate, dict)
            ]
            durable_candidates = [
                candidate
                for candidate in final_candidates
                if str(candidate.get("seed_id") or "") in expected_seed_ids
            ]
            success = len(final_candidates)
        else:
            draft_path = self.repo.run_dir(run["run_id"]) / "ozon_collection_draft.json"
            durable_seed_ids: set[str] = set()
            if draft_path.exists():
                try:
                    draft = self.repo.load_ozon_collection_draft(run["run_id"])
                    durable_candidates = [
                        candidate
                        for candidate in draft.get("ozon_candidates", [])
                        if isinstance(candidate, dict)
                        and str(candidate.get("seed_id") or "") in expected_seed_ids
                    ]
                    durable_seed_ids = {
                        str(candidate.get("seed_id") or "")
                        for candidate in durable_candidates
                    }
                except (json.JSONDecodeError, OSError, TypeError, ValueError):
                    durable_candidates = []
                    durable_seed_ids = set()
            if durable_seed_ids:
                success = len(durable_seed_ids)
            else:
                try:
                    success = int(live.get("success_count", 0) or 0)
                except (TypeError, ValueError):
                    success = 0
        success = min(total, max(0, success))
        failure = min(len(failed), max(total - success, 0))
        processed = min(total, success + failure)
        template_validated_count = 0
        if durable_candidates:
            try:
                template_payload = self.repo.load_attribute_template_result(
                    run["run_id"]
                )
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                template_payload = {}
            templates_by_seed = {
                str(item.get("seed_id") or ""): item
                for item in template_payload.get("seed_templates", [])
                if isinstance(item, dict) and item.get("seed_id")
            }
            for candidate in durable_candidates:
                seed_id = str(candidate.get("seed_id") or "")
                template = templates_by_seed.get(seed_id) or {}
                try:
                    candidate_revision = int(
                        candidate.get("candidate_revision") or 0
                    )
                    template_revision = int(
                        template.get("candidate_revision") or 0
                    )
                except (TypeError, ValueError):
                    continue
                if (
                    str(template.get("source_ozon_product_id") or "")
                    == str(candidate.get("ozon_product_id") or "")
                    and str(template.get("slot_id") or "")
                    == str(candidate.get("slot_id") or "")
                    and candidate_revision > 0
                    and template_revision == candidate_revision
                ):
                    template_validated_count += 1
        collection_complete = total > 0 and success == total and failure == 0
        template_pending_count = max(total - template_validated_count, 0)
        stage_complete = bool(
            collection_complete
            and template_validated_count == total
            and run.get("attribute_template_collected")
            and not run.get("replacement_pending_seed_ids")
        )
        return {
            "total_count": total,
            "processed_count": processed,
            "success_count": success,
            "failure_count": failure,
            "replacement_count": len(replaced),
            "pending_count": max(total - processed, 0),
            "collection_complete": collection_complete,
            "template_validated_count": template_validated_count,
            "template_pending_count": template_pending_count,
            "stage_complete": stage_complete,
        }

    def _run_progress(self, run: dict) -> dict:
        seeds = self._safe_load_sampled_seeds(run["run_id"])
        return {
            "sampled_seed_count": len(seeds),
            "query_ready_count": sum(1 for seed in seeds if seed_has_generated_ozon_query(seed)),
            "needs_query_generation_seed_ids": [
                seed.seed_id for seed in seeds if not seed_has_generated_ozon_query(seed)
            ],
            "attribute_template_contract_ready": bool(run.get("attribute_template_contract_ready")),
            "attribute_template_collected": bool(run.get("attribute_template_collected")),
            "ozon_collection_progress": self._ozon_collection_progress(run, seeds),
        }

    def _supplier_sku_selection_status(self, run_id: str) -> dict[str, Any]:
        supplier_products = [
            item
            for item in self.repo.load_supplier_collection_result(run_id).get("supplier_products", [])
            if isinstance(item, dict) and str(item.get("seed_id") or "")
        ]
        selection_path = self.repo.run_dir(run_id) / "supplier_sku_selections.json"
        stored = self.repo.load_supplier_sku_selections(run_id) if selection_path.exists() else {}
        selections = stored.get("selections") if isinstance(stored.get("selections"), dict) else {}
        missing_seed_ids: list[str] = []
        invalid: list[dict[str, Any]] = []
        for product in supplier_products:
            seed_id = str(product.get("seed_id") or "")
            raw_receipt = selections.get(seed_id)
            if not isinstance(raw_receipt, dict):
                missing_seed_ids.append(seed_id)
                continue
            try:
                receipt = SupplierSkuSelectionReceipt.from_dict(raw_receipt)
            except (KeyError, TypeError, ValueError) as exc:
                invalid.append({"seed_id": seed_id, "reason": f"receipt_parse_failed:{exc}"})
                continue
            if not receipt.verify_hash():
                invalid.append({"seed_id": seed_id, "reason": "selection_hash_mismatch"})
                continue
            current_option = next(
                (
                    option
                    for option in self._supplier_sku_options(product)
                    if isinstance(option, dict)
                    and str(option.get("supplier_sku_id") or "") == receipt.supplier_sku_id
                ),
                None,
            )
            if current_option is None or SupplierSkuOption.from_dict(current_option).to_dict() != receipt.supplier_sku.to_dict():
                invalid.append({"seed_id": seed_id, "reason": "selection_no_longer_matches_collected_sku"})
        return {
            "expected_count": len(supplier_products),
            "selected_count": len(supplier_products) - len(missing_seed_ids) - len(invalid),
            "missing_seed_ids": missing_seed_ids,
            "invalid": invalid,
            "complete": bool(supplier_products) and not missing_seed_ids and not invalid,
        }

    def _image_generation_queue(self) -> ImageGenerationQueue:
        return ImageGenerationQueue(
            self.repo.context.runtime_root / "image_generation" / "ozon_image_jobs.sqlite3"
        )

    def _image_job_payload(self, job_id: str) -> dict[str, Any]:
        snapshot = self._image_generation_queue().snapshot(job_id)
        slots: list[dict[str, Any]] = []
        mapping_keys = (
            "reference_mapping_version",
            "primary_ozon_reference_sha256",
            "reference_slot_index",
            "reference_reused",
            "reference_composition_followed",
            "reference_layout_archetype",
            "reference_layout_followed",
            "locked_subject_preserved",
        )
        for raw_slot in snapshot["slots"]:
            slot = dict(raw_slot)
            raw_receipt = slot.get("receipt_json")
            try:
                receipt = (
                    json.loads(raw_receipt)
                    if isinstance(raw_receipt, str) and raw_receipt.strip()
                    else raw_receipt
                )
            except json.JSONDecodeError:
                receipt = None
            validation = (
                receipt.get("validation")
                if isinstance(receipt, dict)
                and isinstance(receipt.get("validation"), dict)
                else {}
            )
            mapping = {
                key: validation[key]
                for key in mapping_keys
                if key in validation
            }
            if mapping:
                slot["ozon_reference_mapping"] = mapping
            slots.append(slot)
        return {
            **snapshot["job"],
            "slots": slots,
            "attempts": snapshot["attempts"],
        }

    def _download_supplier_image(self, source_url: str, target: Path) -> Path:
        parsed = urlparse(source_url)
        hostname = (parsed.hostname or "").lower()
        allowed_hosts = ("1688.com", "alicdn.com", "tbcdn.cn", "alibaba.com")
        if parsed.scheme not in {"http", "https"} or not any(
            hostname == suffix or hostname.endswith(f".{suffix}") for suffix in allowed_hosts
        ):
            raise ValueError("subject master URL is not an approved 1688 or Alibaba image host")
        request = Request(
            source_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
                ),
                "Referer": "https://detail.1688.com/",
            },
        )
        opener = build_opener(ProxyHandler({}))
        target.parent.mkdir(parents=True, exist_ok=True)
        byte_count = 0
        try:
            with opener.open(request, timeout=30) as response, target.open("wb") as output:
                content_type = str(response.headers.get("Content-Type") or "").lower()
                if content_type and not content_type.startswith("image/"):
                    raise ValueError("subject master URL did not return an image")
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    byte_count += len(chunk)
                    if byte_count > 20 * 1024 * 1024:
                        raise ValueError("subject master image exceeds the 20 MB limit")
                    output.write(chunk)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        if byte_count == 0:
            target.unlink(missing_ok=True)
            raise ValueError("subject master image is empty")
        return target

    def _safe_load_sampled_seeds(self, run_id: str) -> list[SeedProduct]:
        path = self.repo.run_dir(run_id) / "sampled_seeds.json"
        if not path.exists():
            return []
        return self.repo.load_sampled_seeds(run_id)

    def _pending_or_all_seed_ids(
        self,
        run: dict[str, Any],
        all_seed_ids: list[str],
        *,
        result_kind: str,
    ) -> list[str]:
        pending_ids = [
            str(seed_id)
            for seed_id in run.get("replacement_pending_seed_ids", [])
            if str(seed_id) in all_seed_ids
        ]
        if not pending_ids:
            return all_seed_ids

        try:
            if result_kind == "attribute_template":
                retained_result = self.repo.load_attribute_template_result(run["run_id"])
                retained_items = retained_result.get("seed_templates", [])
            elif result_kind == "ozon_collection":
                retained_result = self.repo.load_ozon_collection_result(run["run_id"])
                retained_items = retained_result.get("ozon_candidates", [])
            else:
                raise ValueError(f"Unsupported result kind: {result_kind}")
        except (FileNotFoundError, json.JSONDecodeError):
            return all_seed_ids

        retained_seed_ids = {
            str(item.get("seed_id") or "")
            for item in retained_items
            if isinstance(item, dict) and item.get("seed_id")
        }
        required_retained_ids = set(all_seed_ids) - set(pending_ids)
        if not required_retained_ids.issubset(retained_seed_ids):
            return all_seed_ids
        return pending_ids

    @staticmethod
    def _is_full_current_seed_snapshot(
        items: list[dict[str, Any]],
        all_seed_ids: list[str],
    ) -> bool:
        submitted_seed_ids = [
            str(item.get("seed_id") or "").strip()
            for item in items
        ]
        return bool(
            all_seed_ids
            and len(submitted_seed_ids) == len(all_seed_ids)
            and set(submitted_seed_ids) == set(all_seed_ids)
        )

    def _build_supplier_review(
        self,
        run_id: str,
        payload: dict[str, Any],
        *,
        preserve_created_at: bool = False,
    ) -> dict[str, Any]:
        try:
            existing_review = self.repo.load_supplier_review(run_id)
        except FileNotFoundError:
            existing_review = {}
        existing_by_seed = {
            str(item.get("seed_id") or ""): item
            for item in existing_review.get("items", [])
            if isinstance(item, dict)
        }
        items: list[dict[str, Any]] = []
        for candidate in payload.get("ozon_candidates", []):
            media = candidate.get("selected_sku_media") or {}
            selected_options = (candidate.get("target_sku") or {}).get("selected_options") or {}
            raw_images = [
                *(media.get("selected_sku_images") or []),
                *(media.get("main_gallery_images") or []),
            ]
            images: list[str] = []
            seen_image_keys: set[str] = set()
            for raw_image in raw_images:
                image_url = re.sub(r"/wc\d+/", "/", str(raw_image or "").strip())
                if not image_url.startswith("https://"):
                    continue
                image_key = image_url.rsplit("/", 1)[-1].lower()
                if not image_key or image_key in seen_image_keys:
                    continue
                seen_image_keys.add(image_key)
                images.append(image_url)
                if len(images) >= 5:
                    break
            existing = existing_by_seed.get(str(candidate.get("seed_id") or ""), {})
            items.append(
                {
                    "seed_id": candidate.get("seed_id"),
                    "ozon_product_id": candidate.get("ozon_product_id"),
                    "ozon_title": candidate.get("title"),
                    "ozon_url": candidate.get("ozon_url"),
                    "ozon_main_image": images[0] if images else None,
                    "ozon_reference_images": images,
                    "selected_options": selected_options,
                    "dimension_evidence": self._dimension_evidence(selected_options, candidate.get("attributes") or {}),
                    "key_attributes": candidate.get("attributes") or {},
                    "supplier_url": existing.get("supplier_url"),
                    "user_verified_exact_match": existing.get("user_verified_exact_match") is True,
                    "verified_at": existing.get("verified_at"),
                }
            )
        now = utc_now_iso()
        return {
            "run_id": run_id,
            "items": items,
            "created_at": (
                existing_review.get("created_at")
                if preserve_created_at and existing_review.get("created_at")
                else now
            ),
            "updated_at": now,
        }

    def recover_browser_task_state(self, run_id: str) -> dict[str, Any]:
        with self._run_mutation_lock(run_id):
            run = self.repo.load_run(run_id)
            if not self.repo.is_current_workbench_run(run, directory_name=run_id):
                return run
            return self._restore_completed_ozon_collection_if_valid(run)

    def _restore_completed_ozon_collection_if_valid(self, run: dict[str, Any]) -> dict[str, Any]:
        current = WorkbenchState(run["status"])
        recoverable_states = {
            WorkbenchState.SEED_SELECTED,
            WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTING,
            WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTED,
            WorkbenchState.OZON_COLLECTING,
            WorkbenchState.OZON_COLLECTED,
            WorkbenchState.SUPPLIER_REVIEW,
            WorkbenchState.NEEDS_MANUAL_REVIEW,
            WorkbenchState.FAILED_RETRYABLE,
            WorkbenchState.FAILED_BLOCKED,
        }
        if current not in recoverable_states:
            return run
        try:
            payload = self.repo.load_ozon_collection_result(run["run_id"])
            expected_seed_ids = [seed.seed_id for seed in self._safe_load_sampled_seeds(run["run_id"])]
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
            return run
        if not expected_seed_ids or validate_ozon_collection_result(payload, expected_seed_ids):
            return run

        template_ready = not self._seller_attribute_template_errors(
            run["run_id"],
            expected_seed_ids,
        )
        if template_ready:
            target_state = WorkbenchState.SUPPLIER_REVIEW
        elif (
            current == WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTING
            and run.get("attribute_template_contract_ready")
        ):
            target_state = WorkbenchState.ATTRIBUTE_TEMPLATE_COLLECTING
        else:
            target_state = WorkbenchState.OZON_COLLECTED
        changed = (
            current != target_state
            or run.get("ozon_collected") is not True
            or (template_ready and bool(run.get("replacement_pending_seed_ids")))
        )
        if not changed:
            return run
        run["ozon_collected"] = True
        run["ozon_collection_result_path"] = str(
            self.repo.run_dir(run["run_id"]) / "ozon_collection_result.json"
        )
        run["status"] = target_state.value
        if template_ready:
            review = self._build_supplier_review(
                run["run_id"],
                payload,
                preserve_created_at=True,
            )
            review_path = self.repo.save_supplier_review(run["run_id"], review)
            run["supplier_review_path"] = str(review_path)
            run.pop("replacement_pending_seed_ids", None)
        else:
            run["attribute_template_collected"] = False
            run["attribute_template_contract_ready"] = False
            run.pop("attribute_template_contract_path", None)
            run.pop("supplier_review_path", None)
        self.repo.save_run(run)
        if changed:
            self.repo.append_run_event(
                run["run_id"],
                "ozon_collection.completed_result_restored",
                "A complete Ozon result was restored to the correct post-lock lifecycle gate.",
                {
                    "candidate_count": len(payload.get("ozon_candidates", [])),
                    "template_ready": template_ready,
                    "target_status": target_state.value,
                },
            )
        return run

    def _dimension_evidence(self, selected_options: dict[str, Any], attributes: dict[str, Any]) -> dict[str, Any]:
        markers = ("size", "dimension", "length", "width", "height", "размер", "длина", "ширина", "высота", "尺寸", "长", "宽", "高")
        combined = {**attributes, **selected_options}
        return {
            str(key): value
            for key, value in combined.items()
            if any(marker in str(key).lower() for marker in markers)
        }

    def _supplier_review_complete(self, review: dict[str, Any]) -> bool:
        items = review.get("items") or []
        return bool(items) and all(
            item.get("user_verified_exact_match") is True and self._is_1688_product_url(str(item.get("supplier_url") or ""))
            for item in items
        )

    def _collection_review_items(
        self,
        run_id: str,
        review: dict[str, Any],
        collection_progress: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        ozon_result = self.repo.load_ozon_collection_result(run_id)
        ozon_by_seed = {
            str(item.get("seed_id") or ""): item
            for item in ozon_result.get("ozon_candidates", [])
            if isinstance(item, dict)
        }
        supplier_by_seed: dict[str, dict[str, Any]] = {}
        supplier_path = self.repo.run_dir(run_id) / "supplier_collection_result.json"
        if supplier_path.exists():
            supplier_result = self.repo.load_supplier_collection_result(run_id)
            supplier_by_seed = {
                str(item.get("seed_id") or ""): item
                for item in supplier_result.get("supplier_products", [])
                if isinstance(item, dict)
            }
        progress_seed_id = str((collection_progress or {}).get("seed_id") or "")
        progress_product = (collection_progress or {}).get("partial_product")
        if progress_seed_id and isinstance(progress_product, dict) and progress_seed_id not in supplier_by_seed:
            supplier_by_seed[progress_seed_id] = progress_product
        selection_path = self.repo.run_dir(run_id) / "supplier_sku_selections.json"
        selections = (
            self.repo.load_supplier_sku_selections(run_id).get("selections", {})
            if selection_path.exists()
            else {}
        )
        subject_path = self.repo.run_dir(run_id) / "subject_masters.json"
        subject_items = (
            self.repo.load_subject_masters(run_id).get("items", {})
            if subject_path.exists()
            else {}
        )

        items: list[dict[str, Any]] = []
        for stored_item in review.get("items", []):
            if not isinstance(stored_item, dict):
                continue
            seed_id = str(stored_item.get("seed_id") or "")
            ozon_product = ozon_by_seed.get(seed_id, {})
            raw_supplier_product = supplier_by_seed.get(seed_id)
            supplier_product = dict(raw_supplier_product) if raw_supplier_product else None
            if supplier_product is not None:
                supplier_product["images"] = self._supplier_product_images(supplier_product)
            supplier_sku_options = self._supplier_sku_options(supplier_product) if supplier_product else []
            supplier_sku_candidates = [
                dict(candidate)
                for candidate in (supplier_product or {}).get("sku_option_candidates") or []
                if isinstance(candidate, dict)
                and str(candidate.get("supplier_sku_id") or "").strip()
            ]
            merged = dict(stored_item)
            merged["ozon_product"] = ozon_product
            merged["supplier_product"] = supplier_product
            merged["supplier_sku_options"] = supplier_sku_options
            merged["supplier_sku_candidates"] = supplier_sku_candidates
            merged["supplier_sku_decision"] = self._supplier_sku_decision(
                ozon_product,
                supplier_sku_options,
            )
            merged["supplier_sku_groups"] = supplier_product.get("sku_groups") or [] if supplier_product else []
            merged["supplier_sku_matrix_status"] = (
                supplier_product.get("sku_matrix_status") if supplier_product else None
            )
            merged["supplier_sku_selection"] = selections.get(seed_id) if isinstance(selections, dict) else None
            subject_entry = subject_items.get(seed_id) if isinstance(subject_items, dict) else None
            merged["subject_master"] = (
                subject_entry.get("subject_master")
                if isinstance(subject_entry, dict) and isinstance(subject_entry.get("subject_master"), dict)
                else None
            )
            merged["ozon_images"] = self._ozon_product_images(ozon_product)
            merged["ozon_attributes"] = ozon_product.get("attributes") or {}
            merged["ozon_completeness"] = self._collection_completeness(
                ozon_product,
                {
                    "title": ozon_product.get("title"),
                    "seller": ozon_product.get("seller_name"),
                    "category": ozon_product.get("category_path"),
                    "sku": ozon_product.get("target_sku"),
                    "images": self._ozon_product_images(ozon_product),
                    "price": ozon_product.get("price"),
                    "rating_or_reviews": ozon_product.get("rating") or ozon_product.get("review_count"),
                    "attributes": ozon_product.get("attributes"),
                    "delivery": ozon_product.get("delivery_origin") or ozon_product.get("seller_evidence"),
                    "cross_border_evidence": ozon_product.get("domestic_seller_decision"),
                },
            )
            supplier = supplier_product or {}
            merged["supplier_completeness"] = self._collection_completeness(
                supplier,
                {
                    "title": supplier.get("title") if self._supplier_title_is_product_title(supplier) else None,
                    "seller": supplier.get("seller"),
                    "sku": supplier.get("sku"),
                    "images": supplier.get("images"),
                    "attributes": supplier.get("attributes"),
                    "domestic_shipping": supplier.get("domestic_shipping_evidence"),
                },
            )
            items.append(merged)
        return items

    def _ozon_product_images(self, product: dict[str, Any]) -> list[str]:
        media = product.get("selected_sku_media") or {}
        images = media.get("selected_sku_images") or media.get("main_gallery_images") or []
        return [str(value) for value in images if value]

    def _supplier_product_images(self, product: dict[str, Any]) -> list[str]:
        raw_images = product.get("images") if isinstance(product.get("images"), list) else []
        filtered: list[str] = []
        owner_groups: dict[str, list[str]] = {}
        for value in raw_images:
            url = str(value or "")
            if not url or re.search(r"\.svg(?:[?#]|$)|-55-tps-|_sum\.(?:jpg|jpeg|png|webp)(?:[?#]|$)", url, re.I):
                continue
            if url in filtered:
                continue
            filtered.append(url)
            owner_match = re.search(r"!!(\d+)-\d+-cib", url, re.I)
            if owner_match:
                owner_groups.setdefault(owner_match.group(1), []).append(url)
        if len(owner_groups) <= 1:
            return filtered
        primary_owner = max(owner_groups, key=lambda key: len(owner_groups[key]))
        return owner_groups[primary_owner]

    def _normalize_subject_master_to_locked_sku(
        self,
        subject_master: dict[str, Any],
        supplier_selection: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        if not isinstance(subject_master, dict) or not subject_master:
            return subject_master, False
        supplier_sku = supplier_selection.get("supplier_sku") or {}
        locked_urls = {
            str(value or "").strip()
            for value in supplier_sku.get("image_urls") or []
            if str(value or "").strip()
        }
        if not locked_urls:
            return subject_master, False
        source_urls = [
            str(value or "").strip()
            for value in (
                subject_master.get("source_image_urls")
                or [subject_master.get("source_image_url")]
            )
            if str(value or "").strip()
        ]
        if source_urls and all(url in locked_urls for url in source_urls):
            return subject_master, False
        source_paths = list(
            subject_master.get("source_paths")
            or [subject_master.get("source_path")]
        )
        selected_pairs = [
            (path, url)
            for path, url in zip(source_paths, source_urls)
            if url in locked_urls and str(path or "").strip()
        ]
        if not selected_pairs:
            return {}, False
        try:
            receipt = SupplierSkuSelectionReceipt.from_dict(supplier_selection)
            repaired = SubjectMasterSelection.create(
                receipt=receipt,
                source_paths=[pair[0] for pair in selected_pairs],
                source_image_urls=[pair[1] for pair in selected_pairs],
                visible_subject_quantity=int(
                    subject_master.get("visible_subject_quantity") or 0
                ),
                white_background_confirmed=bool(
                    subject_master.get("white_background_confirmed")
                ),
                confirmed_at=str(
                    subject_master.get("confirmed_at") or utc_now_iso()
                ),
            ).to_dict()
        except (KeyError, OSError, TypeError, ValueError):
            return {}, False
        return repaired, True

    def _refresh_pending_image_task_subject(
        self,
        *,
        package_id: str,
        run_id: str,
        seed_id: str,
        subject_master: dict[str, Any],
        locked_supplier_sku: dict[str, Any],
    ) -> bool:
        if not package_id:
            return False
        located = self.repo.find_image_task_package(package_id)
        if located is None:
            return False
        path, package = located
        if (
            path.parent.name != "pending"
            or str(package.get("status") or "") != "pending"
            or str(package.get("resume_mode") or "") == "upload_only"
            or str(package.get("run_id") or "") != run_id
            or str(package.get("seed_id") or "") != seed_id
        ):
            return False
        source_url = str(subject_master.get("source_image_url") or "").strip()
        if not source_url:
            return False
        package.setdefault("evidence", {})["subject_master"] = subject_master
        package.setdefault("evidence", {})[
            "locked_supplier_sku"
        ] = locked_supplier_sku
        package.setdefault("bootstrap_image", {})["url"] = source_url
        package.setdefault("bootstrap_image", {})[
            "subject_master_sha256"
        ] = subject_master.get("subject_master_sha256")
        for stale_key in ("failure", "failed_at", "assignment"):
            package.pop(stale_key, None)
        package["evidence_refreshed_at"] = utc_now_iso()
        self.repo.save_image_task_package(package_id, package)
        return True

    @staticmethod
    def _sku_measurement_tokens(value: Any) -> list[str]:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True) if not isinstance(value, str) else value
        normalized = text.translate(str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")).lower()
        unit_aliases = {
            "мл": "ml",
            "ml": "ml",
            "毫升": "ml",
            "мм": "mm",
            "mm": "mm",
            "毫米": "mm",
            "см": "cm",
            "cm": "cm",
            "厘米": "cm",
            "кг": "kg",
            "kg": "kg",
            "千克": "kg",
            "л": "l",
            "l": "l",
            "升": "l",
            "г": "g",
            "g": "g",
            "克": "g",
            "м": "m",
            "m": "m",
            "米": "m",
        }
        units = "毫升|毫米|厘米|千克|мл|ml|мм|mm|см|cm|кг|kg|升|л|l|克|г|g|米|м|m"
        tokens: set[str] = set()

        def number_text(raw: str) -> str:
            number = float(raw.replace(",", "."))
            return str(int(number)) if number.is_integer() else str(number)

        label_unit_pattern = re.compile(
            rf"(?<![a-zа-я])({units})(?![a-zа-я])",
            re.I,
        )

        def collect_labeled_measurements(raw: Any) -> None:
            if isinstance(raw, dict):
                for raw_label, raw_value in raw.items():
                    unit_match = label_unit_pattern.search(str(raw_label).lower())
                    if unit_match and isinstance(raw_value, (str, int, float)):
                        unit = unit_aliases[unit_match.group(1).lower()]
                        for raw_number in re.findall(r"\d+(?:[.,]\d+)?", str(raw_value)):
                            tokens.add(f"{number_text(raw_number)}{unit}")
                    collect_labeled_measurements(raw_value)
            elif isinstance(raw, (list, tuple, set)):
                for item in raw:
                    collect_labeled_measurements(item)

        collect_labeled_measurements(value)

        series_pattern = re.compile(
            rf"(?<!\d)((?:\d+(?:[.,]\d+)?\s*[/×xх]\s*)+\d+(?:[.,]\d+)?)\s*({units})(?![a-zа-я])",
            re.I,
        )
        for match in series_pattern.finditer(normalized):
            unit = unit_aliases[match.group(2).lower()]
            for raw_number in re.findall(r"\d+(?:[.,]\d+)?", match.group(1)):
                tokens.add(f"{number_text(raw_number)}{unit}")

        single_pattern = re.compile(
            rf"(?<![\d/×xх])(\d+(?:[.,]\d+)?)\s*({units})(?![a-zа-я])",
            re.I,
        )
        for match in single_pattern.finditer(normalized):
            unit = unit_aliases[match.group(2).lower()]
            tokens.add(f"{number_text(match.group(1))}{unit}")
        return sorted(tokens)

    def _supplier_sku_decision(
        self,
        ozon_product: dict[str, Any],
        options: list[dict[str, Any]],
    ) -> dict[str, Any]:
        target_source = {
            "title": ozon_product.get("title"),
            "selected_options": (ozon_product.get("target_sku") or {}).get("selected_options") or {},
            "attributes": ozon_product.get("attributes") or {},
        }
        target_measurements = self._sku_measurement_tokens(target_source)
        target_token_set = set(target_measurements)
        candidate_decisions: dict[str, dict[str, Any]] = {}
        ranked: list[tuple[int, str]] = []
        ordered_sku_ids: list[str] = []

        for option in options:
            supplier_sku_id = str(option.get("supplier_sku_id") or "")
            option_source = {
                "raw_label": option.get("raw_label"),
                "selected_options": option.get("selected_options") or {},
                "set_composition": option.get("set_composition") or [],
            }
            measurements = self._sku_measurement_tokens(option_source)
            matched_measurements = sorted(target_token_set.intersection(measurements))
            candidate_decisions[supplier_sku_id] = {
                "measurements": measurements,
                "matched_measurements": matched_measurements,
                "score": len(matched_measurements),
            }
            ordered_sku_ids.append(supplier_sku_id)
            ranked.append((len(matched_measurements), supplier_sku_id))

        ranked.sort(reverse=True)
        best_score = ranked[0][0] if ranked else 0
        matching_sku_ids = [
            supplier_sku_id
            for supplier_sku_id in ordered_sku_ids
            if best_score > 0 and candidate_decisions[supplier_sku_id]["score"] == best_score
        ]
        other_sku_ids = [
            supplier_sku_id
            for supplier_sku_id in ordered_sku_ids
            if supplier_sku_id not in matching_sku_ids
        ]
        recommended_sku_id = ""
        if ranked and ranked[0][0] > 0 and (len(ranked) == 1 or ranked[0][0] > ranked[1][0]):
            recommended_sku_id = ranked[0][1]
        single_option_supplier_sku_id = (
            ordered_sku_ids[0] if len(ordered_sku_ids) == 1 else ""
        )
        return {
            "canonical_option_count": len(ordered_sku_ids),
            "single_option_confirmable": len(ordered_sku_ids) == 1,
            "single_option_supplier_sku_id": single_option_supplier_sku_id,
            "target_measurements": target_measurements,
            "matching_sku_ids": matching_sku_ids,
            "other_sku_ids": other_sku_ids,
            "recommended_sku_id": recommended_sku_id,
            "candidates": candidate_decisions,
        }

    @staticmethod
    def _supplier_product_missing_fields(product: dict[str, Any]) -> list[str]:
        required = {
            "title": product.get("title"),
            "seller": product.get("seller"),
            "sku": product.get("sku"),
            "images": product.get("images"),
            "domestic_shipping_evidence": product.get("domestic_shipping_evidence"),
        }
        return [field for field, value in required.items() if value in (None, "", [], {})]

    @staticmethod
    def _normalize_supplier_sku_matrix(
        product: dict[str, Any],
        *,
        allow_deferred_sku: bool,
    ) -> tuple[dict[str, Any], list[str]]:
        normalized = dict(product)
        raw_options = product.get("sku_options") if isinstance(product.get("sku_options"), list) else []
        recovered_options, cleaned_attributes = WorkbenchService._recover_specification_table_skus(product)
        if recovered_options:
            normalized["attributes"] = cleaned_attributes
            if not raw_options:
                raw_options = recovered_options
                normalized["sku"] = {
                    "selected_options": {
                        "visible_sku_labels": [
                            str(option.get("raw_label") or "")
                            for option in recovered_options[:12]
                        ]
                    },
                    "evidence": "specification_table_sku_rows",
                    "evidence_source": "dom_specification_table",
                    "complete": False,
                }
        valid_options: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        errors: list[str] = []
        seen_supplier_sku_ids: set[str] = set()
        for index, raw_option in enumerate(raw_options):
            if not isinstance(raw_option, dict):
                normalized_option_payload: dict[str, Any] | None = None
                option_errors = ["must be an object"]
            else:
                normalized_option_payload = WorkbenchService._normalize_supplier_sku_selection_option(
                    raw_option
                )
                try:
                    option = SupplierSkuOption.from_dict(normalized_option_payload)
                except (TypeError, ValueError) as exc:
                    option_errors = [f"could not be parsed: {exc}"]
                else:
                    option_errors = validate_supplier_sku_option(option)
                    if option.supplier_sku_id in seen_supplier_sku_ids:
                        option_errors = [*option_errors, f"duplicate supplier_sku_id: {option.supplier_sku_id}"]
                    if not option_errors:
                        seen_supplier_sku_ids.add(option.supplier_sku_id)
                        valid_options.append(option.to_dict())
                        continue
            if allow_deferred_sku:
                candidate = (
                    dict(normalized_option_payload)
                    if normalized_option_payload is not None
                    else {"raw_value": raw_option}
                )
                candidate["validation_errors"] = option_errors
                candidates.append(candidate)
            else:
                errors.extend(f"sku_options[{index}] {error}" for error in option_errors)
        normalized["sku_options"] = valid_options
        if candidates:
            normalized["sku_option_candidates"] = candidates
        else:
            normalized.pop("sku_option_candidates", None)
        normalized["sku_matrix_status"] = (
            "complete" if valid_options and not candidates else "manual_confirmation_required"
        )
        return normalized, errors

    @staticmethod
    def _repair_supplier_sku_option_quantity(
        raw_option: dict[str, Any],
    ) -> dict[str, Any]:
        normalized = dict(raw_option)
        composition = [
            str(value).strip()
            for value in normalized.get("set_composition") or []
            if str(value).strip()
        ]
        documented_quantity = documented_composition_quantity(composition)
        if documented_quantity is None:
            return normalized
        try:
            current_quantity = int(normalized.get("set_quantity") or 0)
        except (TypeError, ValueError):
            current_quantity = 0
        if current_quantity == documented_quantity:
            return normalized
        normalized["set_quantity"] = documented_quantity
        evidence = (
            dict(normalized.get("evidence") or {})
            if isinstance(normalized.get("evidence"), dict)
            else {}
        )
        evidence["set_quantity_recovered_from_composition"] = True
        evidence["captured_set_quantity"] = current_quantity
        normalized["evidence"] = evidence
        normalized.pop("validation_errors", None)
        return normalized

    @staticmethod
    def _normalize_supplier_sku_selection_option(
        raw_option: dict[str, Any],
    ) -> dict[str, Any]:
        normalized = WorkbenchService._repair_supplier_sku_option_quantity(
            raw_option
        )
        if normalized.get("complete") is True:
            return normalized
        if str(normalized.get("evidence_source") or "") not in {
            "embedded_sku_map",
            "dom_single_group_sku",
            "dom_single_axis_sku",
            "dom_specification_table",
            "single_sku_detail_page",
            "single_visible_sku_combination",
        }:
            return normalized
        promoted = dict(normalized)
        promoted["complete"] = True
        promoted.pop("validation_errors", None)
        evidence = (
            dict(promoted.get("evidence") or {})
            if isinstance(promoted.get("evidence"), dict)
            else {}
        )
        evidence["price_independent_sku_selection"] = True
        promoted["evidence"] = evidence
        try:
            option = SupplierSkuOption.from_dict(promoted)
        except (TypeError, ValueError):
            return normalized
        return promoted if not validate_supplier_sku_option(option) else normalized

    @staticmethod
    def _recover_specification_table_skus(
        product: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        attributes = product.get("attributes")
        if not isinstance(attributes, dict):
            return [], {}
        entries = [
            (str(key).strip().rstrip("：:"), str(value).strip())
            for key, value in attributes.items()
            if str(key).strip() and str(value).strip()
        ]
        dimension_pattern = re.compile(
            r"^(全长|长度|宽度|高度|直径|尺寸)\s*(?:[（(]\s*(mm|cm|毫米|厘米)\s*[)）])?$",
            re.I,
        )
        header_index = -1
        header_key = ""
        header_value = ""
        dimension_label = ""
        dimension_unit = "毫米"
        for index, (key, value) in enumerate(entries):
            dimension_match = dimension_pattern.fullmatch(value)
            if key not in {"型号", "款号", "货号", "产品规格"} or not dimension_match:
                continue
            header_index = index
            header_key = key
            header_value = value
            dimension_label = dimension_match.group(1)
            raw_unit = str(dimension_match.group(2) or "").casefold()
            dimension_unit = "厘米" if raw_unit in {"cm", "厘米"} else "毫米"
            break
        if header_index < 0:
            return [], dict(attributes)

        row_pattern = re.compile(
            r"^([a-z0-9][a-z0-9._/-]{1,31})\s*[（(]\s*(.{2,120}?)\s*[)）]$",
            re.I,
        )
        value_pattern = re.compile(r"^(\d+(?:\.\d+)?)\s*(mm|cm|毫米|厘米)?$", re.I)
        rows: list[dict[str, str]] = []
        consumed_keys = {header_key}
        for key, value in entries[header_index + 1 :]:
            row_match = row_pattern.fullmatch(key)
            value_match = value_pattern.fullmatch(value)
            if not row_match or not value_match:
                continue
            raw_unit = str(value_match.group(2) or "").casefold()
            unit = "厘米" if raw_unit in {"cm", "厘米"} else dimension_unit
            rows.append(
                {
                    "source_key": key,
                    "source_value": value,
                    "model": row_match.group(1),
                    "specification": row_match.group(2).strip(),
                    "measurement": f"{value_match.group(1)}{unit}",
                }
            )
            consumed_keys.add(key)
        if len(rows) < 2:
            return [], dict(attributes)

        def is_matrix_artifact(key: Any, value: Any) -> bool:
            clean_key = str(key).strip().rstrip("：:")
            clean_value = str(value).strip()
            if clean_key in consumed_keys:
                return True
            if clean_key in {"型号", "款号", "货号", "产品规格"} and dimension_pattern.fullmatch(clean_value):
                return True
            if re.match(r"^[a-z0-9][a-z0-9._/-]{1,31}\s*[（(]", clean_key, flags=re.I):
                return True
            return clean_value.startswith(("全部", "全选", "不限")) or clean_value.endswith(
                ("展开参数", "收起参数")
            )

        cleaned_attributes = {
            key: value
            for key, value in attributes.items()
            if not is_matrix_artifact(key, value)
        }
        offer_id = str(product.get("offer_id") or product.get("supplier_product_id") or "").strip()
        if not offer_id:
            supplier_url = str(product.get("final_url") or product.get("supplier_url") or "")
            offer_match = re.search(r"/offer/(\d+)\.html", supplier_url)
            offer_id = offer_match.group(1) if offer_match else ""
        price_payload = dict(product.get("price") or {}) if isinstance(product.get("price"), dict) else {}
        visible_price = str(price_payload.get("visible_text") or product.get("price") or "")
        amount = str(price_payload.get("amount") or "").strip()
        if not amount:
            amount_match = re.search(r"\d+(?:\.\d+)?", visible_price.replace(",", ""))
            amount = amount_match.group(0) if amount_match else ""
        images = [
            str(value).strip()
            for value in (product.get("images") or [])
            if str(value).strip()
            and not re.search(r"\.svg(?:[?#]|$)|-55-tps-|_sum\.(?:jpg|jpeg|png|webp)(?:[?#]|$)", str(value), re.I)
        ]
        if not offer_id or not images:
            return [], cleaned_attributes

        options: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            raw_label = " / ".join(
                [row["model"], row["specification"], row["measurement"]]
            )
            options.append(
                SupplierSkuOption(
                    supplier_sku_id=f"spec-{offer_id}-{index + 1}-{row['model']}",
                    combination_key=raw_label,
                    raw_label=raw_label,
                    selected_options={
                        "型号": row["model"],
                        "规格": row["specification"],
                        dimension_label: row["measurement"],
                    },
                    set_quantity=1,
                    set_composition=["单件商品"],
                    price={
                        "currency": str(price_payload.get("currency") or "CNY"),
                        "amount": amount,
                    },
                    stock={"status": "unknown", "quantity": None},
                    image_urls=[images[0]],
                    evidence_source="dom_specification_table",
                    complete=True,
                    evidence={
                        "offer_id": offer_id,
                        "header_key": header_key,
                        "header_value": header_value,
                        "source_row_key": row["source_key"],
                        "source_row_value": row["source_value"],
                        "recovered_from_collected_attributes": True,
                        "price_visible_text": visible_price,
                    },
                ).to_dict()
            )
        return options, cleaned_attributes

    def _single_visible_supplier_sku_option(
        self,
        product: dict[str, Any],
    ) -> dict[str, Any] | None:
        sku_groups = product.get("sku_groups")
        if not isinstance(sku_groups, list) or not sku_groups:
            return None
        selected_options: dict[str, str] = {}
        selected_group_options: list[dict[str, Any]] = []
        for index, raw_group in enumerate(sku_groups):
            if not isinstance(raw_group, dict):
                return None
            raw_group_options = raw_group.get("options")
            if not isinstance(raw_group_options, list):
                return None
            enabled_options = [
                option
                for option in raw_group_options
                if isinstance(option, dict) and option.get("disabled") is not True
            ]
            if len(enabled_options) != 1:
                return None
            selected = enabled_options[0]
            group_name = str(
                raw_group.get("name")
                or raw_group.get("label")
                or raw_group.get("group_name")
                or f"规格{index + 1}"
            ).strip()
            label = str(
                selected.get("label")
                or selected.get("value")
                or selected.get("name")
                or ""
            ).strip()
            if not group_name or not label:
                return None
            selected_options[group_name] = label
            selected_group_options.append(selected)

        raw_candidates = [
            candidate
            for candidate in product.get("sku_option_candidates") or []
            if isinstance(candidate, dict)
        ]
        candidate = raw_candidates[0] if len(raw_candidates) == 1 else {}
        native_ids = {
            str(option.get("supplier_sku_id") or "").strip()
            for option in selected_group_options
            if str(option.get("supplier_sku_id") or "").strip()
        }
        supplier_sku_id = str(candidate.get("supplier_sku_id") or "").strip()
        if not supplier_sku_id and len(native_ids) == 1:
            supplier_sku_id = next(iter(native_ids))

        offer_id = str(
            product.get("offer_id")
            or product.get("supplier_product_id")
            or ""
        ).strip()
        if not offer_id:
            supplier_url = str(
                product.get("final_url")
                or product.get("supplier_url")
                or ""
            )
            offer_match = re.search(r"/offer/(\d+)\.html", supplier_url)
            offer_id = offer_match.group(1) if offer_match else ""
        combination_key = str(candidate.get("combination_key") or "").strip()
        if not combination_key:
            combination_key = "|".join(
                f"{name}>{value}" for name, value in selected_options.items()
            )
        if not supplier_sku_id and offer_id:
            digest = hashlib.sha256(
                combination_key.encode("utf-8")
            ).hexdigest()[:12]
            supplier_sku_id = f"visible-{offer_id}-{digest}"
        if not supplier_sku_id:
            return None

        image_urls: list[str] = []
        for raw_url in [
            *(option.get("image_url") for option in selected_group_options),
            *(candidate.get("image_urls") or []),
            *self._supplier_product_images(product),
        ]:
            url = str(raw_url or "").strip()
            if url and url not in image_urls:
                image_urls.append(url)
        if not image_urls:
            return None

        composition = [
            str(value).strip()
            for value in candidate.get("set_composition") or []
            if str(value).strip()
        ] or list(selected_options.values())
        documented_quantity = documented_composition_quantity(composition)
        try:
            candidate_quantity = int(candidate.get("set_quantity") or 0)
        except (TypeError, ValueError):
            candidate_quantity = 0
        # A single visible page SKU is one sales unit unless its selected label
        # or composition explicitly documents a multi-item set. Raw numeric
        # metadata is not trusted here because age, size and model numbers have
        # repeatedly been misclassified as quantity.
        set_quantity = documented_quantity or 1
        price = (
            dict(candidate.get("price") or {})
            if isinstance(candidate.get("price"), dict)
            else (
                dict(product.get("price") or {})
                if isinstance(product.get("price"), dict)
                else {}
            )
        )
        stock = (
            dict(candidate.get("stock") or {})
            if isinstance(candidate.get("stock"), dict)
            else {}
        )
        if not str(stock.get("status") or "").strip():
            stock["status"] = "unknown"
            stock.setdefault("quantity", None)
        evidence = (
            dict(candidate.get("evidence") or {})
            if isinstance(candidate.get("evidence"), dict)
            else {}
        )
        evidence.update(
            {
                "single_visible_combination": True,
                "group_names": list(selected_options),
                "native_supplier_sku_id": bool(
                    candidate.get("supplier_sku_id") or native_ids
                ),
                "user_confirmation_required": True,
                "price_independent_sku_selection": True,
            }
        )
        if candidate_quantity > 0 and candidate_quantity != set_quantity:
            evidence["ignored_unverified_candidate_quantity"] = True
            evidence["captured_candidate_quantity"] = candidate_quantity
        option = SupplierSkuOption(
            supplier_sku_id=supplier_sku_id,
            combination_key=combination_key,
            raw_label=str(candidate.get("raw_label") or "").strip()
            or " / ".join(selected_options.values()),
            selected_options=selected_options,
            set_quantity=set_quantity,
            set_composition=composition,
            price=price,
            stock=stock,
            image_urls=image_urls,
            evidence_source=str(candidate.get("evidence_source") or "").strip()
            or "single_visible_sku_combination",
            complete=True,
            evidence=evidence,
        )
        return option.to_dict() if not validate_supplier_sku_option(option) else None

    def _supplier_sku_options(self, product: dict[str, Any]) -> list[dict[str, Any]]:
        raw_options = product.get("sku_options")
        if isinstance(raw_options, list) and raw_options:
            normalized_options: list[dict[str, Any]] = []
            for raw_option in raw_options:
                if not isinstance(raw_option, dict):
                    continue
                normalized = self._normalize_supplier_sku_selection_option(
                    raw_option
                )
                try:
                    option = SupplierSkuOption.from_dict(normalized)
                except (TypeError, ValueError):
                    continue
                if not validate_supplier_sku_option(option):
                    normalized_options.append(option.to_dict())
            if normalized_options:
                return normalized_options
        raw_candidates = product.get("sku_option_candidates")
        if isinstance(raw_candidates, list) and raw_candidates:
            recovered_candidates: list[dict[str, Any]] = []
            for raw_candidate in raw_candidates:
                if not isinstance(raw_candidate, dict):
                    continue
                repaired = self._normalize_supplier_sku_selection_option(
                    raw_candidate
                )
                try:
                    option = SupplierSkuOption.from_dict(repaired)
                except (TypeError, ValueError):
                    continue
                if not validate_supplier_sku_option(option):
                    recovered_candidates.append(option.to_dict())
            if recovered_candidates:
                return recovered_candidates
        recovered_options, _cleaned_attributes = self._recover_specification_table_skus(product)
        if recovered_options:
            return recovered_options
        sku_groups = product.get("sku_groups")
        if isinstance(sku_groups, list) and sku_groups:
            single_visible_option = self._single_visible_supplier_sku_option(
                product
            )
            if single_visible_option:
                return [single_visible_option]
            return []
        sku = product.get("sku") if isinstance(product.get("sku"), dict) else {}
        selected = sku.get("selected_options") if isinstance(sku.get("selected_options"), dict) else {}
        visible_labels = selected.get("visible_sku_labels")
        labels = [str(value) for value in visible_labels] if isinstance(visible_labels, list) else []
        is_no_variant_page = (
            str(sku.get("evidence") or "") == "no_visible_variant_selector"
            or any("单一 SKU" in label and "无可选规格" in label for label in labels)
        )
        if not is_no_variant_page:
            return []

        offer_id = str(product.get("offer_id") or product.get("supplier_product_id") or "").strip()
        if not offer_id:
            supplier_url = str(product.get("final_url") or product.get("supplier_url") or "")
            offer_match = re.search(r"/offer/(\d+)\.html", supplier_url)
            offer_id = offer_match.group(1) if offer_match else ""
        price = product.get("price")
        price_payload = dict(price) if isinstance(price, dict) else {}
        amount = str(price_payload.get("amount") or "").strip()
        visible_price = str(price_payload.get("visible_text") or price or "")
        if not amount:
            amount_match = re.search(r"\d+(?:\.\d+)?", visible_price.replace(",", ""))
            amount = amount_match.group(0) if amount_match else ""
        images = self._supplier_product_images(product)
        if not offer_id or not images:
            return []

        quantity = 1
        quantity_sources = [str(product.get("title") or "")]
        attributes = product.get("attributes")
        if isinstance(attributes, dict):
            quantity_sources.extend(str(value) for value in attributes.values())
        for value in quantity_sources:
            quantity_match = re.search(r"(\d+)\s*(?:支|件|个|只|套|枚|片|瓶|包|组)", value)
            if quantity_match:
                quantity = max(1, int(quantity_match.group(1)))
                break
        composition = [f"{quantity}件装"] if quantity > 1 else ["单件商品"]
        option = SupplierSkuOption(
            supplier_sku_id=offer_id,
            combination_key="页面唯一 SKU",
            raw_label="页面唯一 SKU（无需选择规格）",
            selected_options={"规格": "页面唯一 SKU"},
            set_quantity=quantity,
            set_composition=composition,
            price={
                "currency": str(price_payload.get("currency") or "CNY"),
                "amount": amount,
            },
            stock={"status": "unknown", "quantity": None},
            image_urls=[images[0]],
            evidence_source="single_sku_detail_page",
            complete=True,
            evidence={
                "offer_id": offer_id,
                "no_visible_variant_selector": True,
                "recovered_from_collected_page": True,
                "price_visible_text": visible_price,
            },
        )
        return [option.to_dict()]

    def _supplier_title_is_product_title(self, product: dict[str, Any]) -> bool:
        title = str(product.get("title") or "").strip().casefold()
        seller = product.get("seller")
        seller_name = str((seller or {}).get("shop_name") or "") if isinstance(seller, dict) else str(seller or "")
        return bool(title) and title != seller_name.strip().casefold()

    def _collection_completeness(
        self,
        product: dict[str, Any],
        required_fields: dict[str, Any],
    ) -> dict[str, Any]:
        missing = [key for key, value in required_fields.items() if value in (None, "", [], {})]
        return {
            "complete": bool(product) and not missing,
            "missing_fields": missing,
            "collected_fields": [key for key in required_fields if key not in missing],
        }

    def _is_1688_product_url(self, value: str) -> bool:
        parsed = urlparse(value)
        host = parsed.hostname or ""
        return (
            parsed.scheme in {"http", "https"}
            and (host == "1688.com" or host.endswith(".1688.com"))
            and "/offer/" in parsed.path
            and parsed.path.endswith(".html")
        )

    def _update_query_summary(self, run: dict, seeds: list[SeedProduct]) -> None:
        run["query_ready_count"] = sum(1 for seed in seeds if seed_has_generated_ozon_query(seed))
        run["needs_query_generation_seed_ids"] = [
            seed.seed_id for seed in seeds if not seed_has_generated_ozon_query(seed)
        ]


def _generated_field_results(generated: dict[str, Any]) -> dict[str, dict[str, Any]]:
    structured = generated.get("field_results")
    if isinstance(structured, dict):
        return {
            str(field_key): dict(result)
            for field_key, result in structured.items()
            if isinstance(result, dict)
            and str(result.get("decision") or "") in {"filled", "unresolved"}
        }
    legacy_fields = generated.get("fields")
    if not isinstance(legacy_fields, dict):
        return {}
    return {
        str(field_key): {
            "decision": "filled",
            "value": value,
            "evidence_refs": [],
            "reason": "Previously validated generated content.",
            "mode": "creative_rewrite",
        }
        for field_key, value in legacy_fields.items()
        if _has_content_value(value)
    }


def _content_evidence_index(evidence: dict[str, Any]) -> dict[str, Any]:
    index: dict[str, Any] = {}

    def add(reference: str, value: Any) -> None:
        if _has_content_value(value) or isinstance(value, (dict, list)):
            if value not in (None, "", [], {}):
                index[reference] = value

    add("ozon.title", evidence.get("ozon_title_style_reference"))
    ozon_selected_sku = evidence.get("ozon_selected_sku")
    if isinstance(ozon_selected_sku, dict):
        for label, value in ozon_selected_sku.items():
            add(f"ozon.target_sku.selected_options.{label}", value)
    ozon_attributes = evidence.get("ozon_attributes")
    if isinstance(ozon_attributes, dict):
        for label, value in ozon_attributes.items():
            add(f"ozon.attributes.{label}", value)
    content_evidence = evidence.get("ozon_content_score_evidence")
    if isinstance(content_evidence, dict):
        attribute_table = content_evidence.get("attribute_table")
        if isinstance(attribute_table, dict):
            for label, value in attribute_table.items():
                add(f"ozon.content_score_evidence.attribute_table.{label}", value)
        blocks = content_evidence.get("description_or_rich_content_blocks")
        if isinstance(blocks, list):
            for index_number, value in enumerate(blocks):
                add(
                    "ozon.content_score_evidence."
                    f"description_or_rich_content_blocks.{index_number}",
                    value,
                )
    add("supplier.title", evidence.get("supplier_title"))
    add("supplier.offer_id", evidence.get("supplier_offer_id"))
    supplier_attributes = evidence.get("supplier_attributes")
    if isinstance(supplier_attributes, dict):
        for label, value in supplier_attributes.items():
            add(f"supplier.attributes.{label}", value)
    confirmed_supplier_sku = evidence.get("confirmed_supplier_sku")
    if isinstance(confirmed_supplier_sku, dict):
        for key in (
            "supplier_sku_id",
            "combination_key",
            "raw_label",
            "set_quantity",
            "evidence_source",
            "complete",
        ):
            add(
                f"supplier_selection.supplier_sku.{key}",
                confirmed_supplier_sku.get(key),
            )
        selected_options = confirmed_supplier_sku.get("selected_options")
        if isinstance(selected_options, dict):
            for label, value in selected_options.items():
                add(
                    f"supplier_selection.supplier_sku.selected_options.{label}",
                    value,
                )
        set_composition = confirmed_supplier_sku.get("set_composition")
        if isinstance(set_composition, list):
            for index_number, value in enumerate(set_composition):
                add(
                    "supplier_selection.supplier_sku."
                    f"set_composition.{index_number}",
                    value,
                )
        image_urls = confirmed_supplier_sku.get("image_urls")
        if isinstance(image_urls, list):
            for index_number, value in enumerate(image_urls):
                add(
                    "supplier_selection.supplier_sku."
                    f"image_urls.{index_number}",
                    value,
                )
        for group_name in ("price", "stock"):
            group = confirmed_supplier_sku.get(group_name)
            if not isinstance(group, dict):
                continue
            for key, value in group.items():
                add(
                    f"supplier_selection.supplier_sku.{group_name}.{key}",
                    value,
                )
    supplier_visual_evidence = evidence.get("supplier_visual_evidence")
    if isinstance(supplier_visual_evidence, dict):
        product_images = supplier_visual_evidence.get("product_images")
        if isinstance(product_images, list):
            for index_number, value in enumerate(product_images):
                add(f"supplier.images.{index_number}", value)
    return index


def _field_candidate_evidence_refs(
    field: dict[str, Any],
    evidence_index: dict[str, Any],
) -> list[str]:
    canonical_label = canonical_attribute_label(field.get("label"))
    candidates: list[str] = []
    for reference in evidence_index:
        evidence_label = reference.rsplit(".", 1)[-1]
        if canonical_attribute_label(evidence_label) == canonical_label:
            candidates.append(reference)
    special_suffixes = {
        "quantity": (".set_quantity",),
        "package_contents": (".set_composition.0", ".raw_label"),
        "set_item_count": (".set_quantity", ".set_composition.0", ".raw_label"),
        "model": (".raw_label", ".combination_key"),
        "article": (".supplier_sku_id",),
    }
    related_canonical_labels = {
        "set_item_count": {"package_contents", "quantity"},
        "factory_package_count": {"quantity"},
    }
    candidates.extend(
        reference
        for reference in evidence_index
        if canonical_attribute_label(reference.rsplit(".", 1)[-1])
        in related_canonical_labels.get(canonical_label, set())
    )
    for suffix in special_suffixes.get(canonical_label, ()):
        candidates.extend(
            reference
            for reference in evidence_index
            if reference.endswith(suffix)
        )
    return list(dict.fromkeys(candidates))


def _field_visual_evidence_refs(
    field: dict[str, Any],
    evidence_index: dict[str, Any],
    evidence: dict[str, Any],
) -> list[str]:
    if not is_visual_inference_field(field.get("label")):
        return []
    refs = [
        reference
        for reference in evidence_index
        if reference.startswith(
            "supplier_selection.supplier_sku.image_urls."
        )
    ]
    supplier_visual_evidence = evidence.get("supplier_visual_evidence")
    if (
        isinstance(supplier_visual_evidence, dict)
        and supplier_visual_evidence.get("page_single_sku") is True
    ):
        refs.extend(
            reference
            for reference in evidence_index
            if reference.startswith("supplier.images.")
        )
    return list(dict.fromkeys(refs))


def _visual_evidence_roles(references: list[str]) -> dict[str, str]:
    return {
        reference: (
            "locked_sku_primary"
            if reference.startswith(
                "supplier_selection.supplier_sku.image_urls."
            )
            else "single_sku_gallery"
        )
        for reference in references
    }


def _visual_target_scope(field: dict[str, Any]) -> str | None:
    return {
        "color": "primary_product",
        "factory_package_count": "factory_packaging",
        "set_item_count": "complete_set",
    }.get(canonical_attribute_label(field.get("label")))


def _validated_visual_analysis(
    field: dict[str, Any],
    raw_analysis: Any,
    expected_refs: list[str],
    decision: str,
    *,
    value: Any = None,
    resolution_class: str = "",
) -> tuple[dict[str, Any] | None, list[str]]:
    if not expected_refs:
        return None, []
    label = str(field.get("label") or field.get("field_key") or "Visual field")
    if not isinstance(raw_analysis, dict):
        return None, [
            f"{label}: visual_analysis is required after actual locked-image inspection."
        ]
    result = str(raw_analysis.get("result") or "").strip()
    confidence = str(raw_analysis.get("confidence") or "").strip()
    field_finding = str(raw_analysis.get("field_finding") or "").strip()
    raw_subject_analysis = raw_analysis.get("subject_analysis")
    inspected_refs = [
        str(reference)
        for reference in (raw_analysis.get("inspected_refs") or [])
        if str(reference or "").strip()
    ]
    raw_observations = raw_analysis.get("observations")
    observations: list[dict[str, str]] = []
    errors: list[str] = []
    if result not in {"observed", "not_visible", "ambiguous", "conflict"}:
        errors.append(
            f"{label}: visual_analysis.result must be observed, not_visible, "
            "ambiguous, or conflict."
        )
    if decision == "filled" and result != "observed":
        errors.append(
            f"{label}: a filled visual decision requires result=observed."
        )
    if confidence not in {"high", "medium", "low"}:
        errors.append(
            f"{label}: visual_analysis.confidence must be high, medium, or low."
        )
    if len(field_finding) < 10:
        errors.append(
            f"{label}: visual_analysis.field_finding must describe this field."
        )
    expected_set = set(expected_refs)
    inspected_set = set(inspected_refs)
    if inspected_set != expected_set:
        missing = sorted(expected_set - inspected_set)
        unexpected = sorted(inspected_set - expected_set)
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected {', '.join(unexpected)}")
        errors.append(
            f"{label}: visual_analysis.inspected_refs must cover every locked "
            f"image ({'; '.join(details)})."
        )
    if not isinstance(raw_observations, list):
        errors.append(
            f"{label}: visual_analysis.observations must describe every image."
        )
    else:
        for observation in raw_observations:
            if not isinstance(observation, dict):
                errors.append(
                    f"{label}: each visual observation must be an object."
                )
                continue
            evidence_ref = str(observation.get("evidence_ref") or "").strip()
            finding = str(observation.get("finding") or "").strip()
            if evidence_ref not in expected_set:
                errors.append(
                    f"{label}: visual observation uses unknown evidence_ref "
                    f"{evidence_ref or '<empty>'}."
                )
            if len(finding) < 10:
                errors.append(
                    f"{label}: every visual observation needs a concrete finding."
                )
            observations.append(
                {
                    "evidence_ref": evidence_ref,
                    "finding": finding,
                }
            )
        observed_refs = {
            observation["evidence_ref"]
            for observation in observations
            if observation["evidence_ref"]
        }
        if observed_refs != expected_set:
            errors.append(
                f"{label}: visual observations must cover every locked image."
            )
    subject_analysis, subject_errors = _validated_visual_subject_analysis(
        field,
        raw_subject_analysis,
        expected_refs,
        decision,
        result,
        value=value,
        resolution_class=resolution_class,
    )
    errors.extend(subject_errors)
    normalized = {
        "result": result,
        "confidence": confidence,
        "field_finding": field_finding,
        "inspected_refs": inspected_refs,
        "observations": observations,
        "subject_analysis": subject_analysis,
    }
    return normalized, errors


def _validated_visual_subject_analysis(
    field: dict[str, Any],
    raw_subject: Any,
    expected_refs: list[str],
    decision: str,
    result: str,
    *,
    value: Any = None,
    resolution_class: str = "",
) -> tuple[dict[str, Any] | None, list[str]]:
    label = str(field.get("label") or field.get("field_key") or "Visual field")
    if not isinstance(raw_subject, dict):
        return None, [
            f"{label}: visual_analysis.subject_analysis is required to "
            "separate the primary subject from accessories and packaging."
        ]
    primary_subject = str(raw_subject.get("primary_subject") or "").strip()
    target_scope = str(raw_subject.get("target_scope") or "").strip()
    basis_refs = [
        str(reference)
        for reference in (raw_subject.get("basis_refs") or [])
        if str(reference or "").strip()
    ]
    raw_excluded = raw_subject.get("excluded_elements")
    excluded_elements: list[dict[str, Any]] = []
    errors: list[str] = []
    expected_scope = _visual_target_scope(field)
    expected_set = set(expected_refs)
    if len(primary_subject) < 3:
        errors.append(
            f"{label}: subject_analysis.primary_subject must identify the "
            "locked SKU's product subject."
        )
    if target_scope != expected_scope:
        errors.append(
            f"{label}: subject_analysis.target_scope must be {expected_scope}."
        )
    if not basis_refs or not set(basis_refs).issubset(expected_set):
        errors.append(
            f"{label}: subject_analysis.basis_refs must cite inspected locked "
            "SKU images."
        )
    allowed_roles = {
        "accessory",
        "packaging",
        "background",
        "decoration",
        "text_overlay",
        "reference_variant",
    }
    if not isinstance(raw_excluded, list):
        errors.append(
            f"{label}: subject_analysis.excluded_elements must be a list."
        )
    else:
        for excluded in raw_excluded:
            if not isinstance(excluded, dict):
                errors.append(
                    f"{label}: each excluded visual element must be an object."
                )
                continue
            element = str(excluded.get("element") or "").strip()
            role = str(excluded.get("role") or "").strip()
            colors = [
                str(color).strip()
                for color in (excluded.get("colors") or [])
                if str(color or "").strip()
            ]
            if len(element) < 3:
                errors.append(
                    f"{label}: excluded visual elements need a concrete name."
                )
            if role not in allowed_roles:
                errors.append(
                    f"{label}: excluded visual element role must be one of "
                    f"{', '.join(sorted(allowed_roles))}."
                )
            excluded_elements.append(
                {
                    "element": element,
                    "role": role,
                    "colors": colors,
                }
            )

    canonical_label = canonical_attribute_label(field.get("label"))
    subject_state = str(raw_subject.get("subject_state") or "").strip()
    subject_colors = [
        str(color).strip()
        for color in (raw_subject.get("subject_colors") or [])
        if str(color or "").strip()
    ]
    normalized_value = str(raw_subject.get("normalized_value") or "").strip()
    if canonical_label == "color":
        allowed_states = {
            "single_color",
            "multi_color",
            "variant_conflict",
            "not_visible",
        }
        if subject_state not in allowed_states:
            errors.append(
                f"{label}: color subject_state must be single_color, "
                "multi_color, variant_conflict, or not_visible."
            )
        if subject_state in {"single_color", "multi_color"}:
            if not subject_colors or not normalized_value:
                errors.append(
                    f"{label}: an observed subject color requires "
                    "subject_colors and normalized_value."
                )
            if result != "observed":
                errors.append(
                    f"{label}: {subject_state} is an observed primary-subject "
                    "fact, not an ambiguous accessory conflict."
                )
            allowed_values = [
                str(allowed).strip()
                for allowed in (field.get("allowed_values") or [])
                if str(allowed or "").strip()
            ]
            normalized_allowed = {
                allowed.casefold(): allowed for allowed in allowed_values
            }
            permitted_value = (
                not allowed_values
                or normalized_value.casefold() in normalized_allowed
            )
            if decision == "filled":
                if str(value or "").strip().casefold() != normalized_value.casefold():
                    errors.append(
                        f"{label}: filled value must equal the normalized "
                        "primary-subject color."
                    )
            elif permitted_value:
                errors.append(
                    f"{label}: {subject_state} with a permitted normalized "
                    "value must be filled."
                )
            elif resolution_class != "dictionary_value_missing":
                errors.append(
                    f"{label}: an observed color outside the Seller API "
                    "dictionary must use dictionary_value_missing."
                )
        elif subject_state == "variant_conflict":
            if decision != "unresolved" or resolution_class != "evidence_conflict":
                errors.append(
                    f"{label}: variant_conflict must remain unresolved with "
                    "evidence_conflict."
                )
            if result not in {"ambiguous", "conflict"}:
                errors.append(
                    f"{label}: variant_conflict requires result=ambiguous or conflict."
                )
        elif subject_state == "not_visible":
            if decision != "unresolved" or result != "not_visible":
                errors.append(
                    f"{label}: not_visible must remain an unresolved "
                    "not_visible result."
                )
    elif decision == "unresolved" and result == "observed":
        errors.append(
            f"{label}: an observed visual fact cannot be submitted as unresolved."
        )

    return (
        {
            "primary_subject": primary_subject,
            "target_scope": target_scope,
            "basis_refs": basis_refs,
            "excluded_elements": excluded_elements,
            "subject_state": subject_state,
            "subject_colors": subject_colors,
            "normalized_value": normalized_value,
        },
        errors,
    )


def _content_result_is_accepted(
    field: dict[str, Any],
    result: dict[str, Any] | None,
    *,
    visual_evidence_refs: list[str] | None = None,
) -> bool:
    if not isinstance(result, dict):
        return False
    decision = str(result.get("decision") or "")
    current_visual_refs = list(visual_evidence_refs or [])
    if current_visual_refs:
        _visual_analysis, visual_errors = _validated_visual_analysis(
            field,
            result.get("visual_analysis"),
            current_visual_refs,
            decision,
            value=result.get("value"),
            resolution_class=str(result.get("resolution_class") or ""),
        )
        if visual_errors:
            return False
    if decision == "unresolved":
        if (
            str(result.get("resolution_class") or "")
            not in _CONTENT_RESOLUTION_CLASSES
        ):
            return False
        current_visual_refs_set = set(current_visual_refs)
        prior_refs = {
            str(reference)
            for reference in (result.get("evidence_refs") or [])
            if str(reference or "").strip()
        }
        return not current_visual_refs_set or bool(
            current_visual_refs_set.intersection(prior_refs)
        )
    if decision != "filled" or not _has_content_value(result.get("value")):
        return False
    if (
        _field_requires_russian_objective_text(field)
        and re.search(r"[\u3400-\u9fff]", str(result.get("value") or ""))
    ):
        return False
    return True


def _field_requires_russian_objective_text(field: dict[str, Any]) -> bool:
    return canonical_attribute_label(field.get("label")) in _RUSSIAN_OBJECTIVE_FIELDS


def _has_content_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _coerce_boolean_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().casefold()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    return None


def _normalized_required_attribute_value(
    field: dict[str, Any] | None,
    value: Any,
) -> Any:
    attribute_type = str((field or {}).get("attribute_type") or "").strip().casefold()
    if attribute_type not in {"bool", "boolean"}:
        return value
    normalized = _coerce_boolean_value(value)
    return normalized if normalized is not None else value


def _normalize_content_text(value: Any) -> str:
    return " ".join(
        part
        for part in re.split(r"[^\w]+", str(value or "").casefold().replace("ё", "е"))
        if part
    )


def _candidate_sale_rub(candidate: dict[str, Any]) -> str:
    price = candidate.get("price")
    if isinstance(price, dict):
        price = (
            price.get("amount")
            or price.get("value")
            or price.get("visible_text")
        )
    normalized = re.sub(r"[^\d.,-]", "", str(price or "")).replace(",", ".")
    if not normalized:
        raise ValueError(
            "The collected Ozon reference price is missing for this product."
        )
    return normalized


def _dictionary_upload_value(
    item: dict[str, Any],
    field: dict[str, Any],
) -> Any:
    value = field.get("value")
    canonical = canonical_attribute_label(field.get("label"))
    if canonical == "hashtags":
        return _normalize_ozon_hashtags(value)
    if str(field.get("field_key") or "") == "8229" or canonical == "type":
        category_leaf = str(item.get("category_path") or "").rsplit("/", 1)[-1].strip()
        if category_leaf:
            return category_leaf
    if str(field.get("field_key") or "") == "9782":
        normalized_hazard = str(value or "").strip().casefold()
        if normalized_hazard in {
            "0",
            "0.0",
            "false",
            "none",
            "no",
            "нет",
            "не опасен",
            "not dangerous",
            "无",
            "不危险",
            "非危险品",
        }:
            return "Не опасен"
    if canonical == "brand" and re.search(r"[\u3400-\u9fff]", str(value or "")):
        return "Нет бренда"
    return value


def _normalize_ozon_hashtags(value: Any) -> str:
    tokens: list[str] = []
    seen: set[str] = set()
    for segment in re.findall(r"#([^#]+)", str(value or "")):
        normalized = re.sub(r"[^\w]+", "_", segment, flags=re.UNICODE).strip("_")
        if not normalized:
            continue
        token = f"#{normalized}"
        dedupe_key = token.casefold()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        tokens.append(token)
    return " ".join(tokens)


def _valid_ozon_hashtags(value: Any, *, minimum_count: int = 1) -> bool:
    text = str(value or "").strip()
    tokens = text.split()
    return bool(
        len(tokens) >= minimum_count
        and text == " ".join(tokens)
        and all(re.fullmatch(r"#[\w]+", token, flags=re.UNICODE) for token in tokens)
    )


def _public_reviewed_image_urls(
    values: list[Any],
    *,
    base_url: str = "",
) -> list[str]:
    base_url = str(base_url or "").strip()
    public_base = base_url.rstrip("/") if _is_public_http_url(base_url) else ""
    result: list[str] = []
    for value in values:
        url = str(value or "").strip()
        if not url:
            continue
        if _is_public_http_url(url):
            result.append(url)
        elif public_base and url.startswith("/"):
            result.append(f"{public_base}/{url.lstrip('/')}")
    return result


def _is_public_http_url(value: str) -> bool:
    try:
        parsed = urlparse(str(value or "").strip())
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    hostname = parsed.hostname.casefold()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".local"):
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return True
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
    )


def _is_public_https_url(value: str) -> bool:
    """Validate externally reachable HTTPS evidence URLs.

    This is a generic locked-source image check, not a configurable media
    publishing setting.
    """
    try:
        parsed = urlparse(str(value or "").strip())
    except ValueError:
        return False
    return parsed.scheme == "https" and _is_public_http_url(value)


def _normalize_ozon_rich_content_value(value: Any) -> str:
    if isinstance(value, str):
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("Rich Content must be valid JSON.") from exc
    else:
        payload = value
    if (
        isinstance(payload, dict)
        and payload.get("version") == 0.3
        and isinstance(payload.get("content"), list)
        and payload["content"]
        and all(
            isinstance(widget, dict)
            and str(widget.get("widgetName") or "").strip()
            and isinstance(widget.get("blocks"), list)
            and widget["blocks"]
            for widget in payload["content"]
        )
    ):
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    fragments: list[str] = []

    def collect(node: Any) -> None:
        if isinstance(node, str):
            text = re.sub(r"\s+", " ", node).strip()
            if text:
                fragments.append(text)
            return
        if isinstance(node, list):
            for item in node:
                collect(item)
            return
        if not isinstance(node, dict):
            return
        prioritized_keys = (
            "title",
            "description",
            "heading",
            "text",
            "detail",
            "items",
            "content",
            "blocks",
        )
        for key in prioritized_keys:
            if key in node:
                collect(node[key])

    collect(payload)
    unique_fragments: list[str] = []
    seen_fragments: set[str] = set()
    for fragment in fragments:
        normalized = _normalize_content_text(fragment)
        if normalized in seen_fragments:
            continue
        seen_fragments.add(normalized)
        unique_fragments.append(fragment)
    if not unique_fragments:
        raise ValueError("Rich Content JSON contains no customer-facing text.")
    text = " ".join(
        fragment
        if fragment.endswith((".", "!", "?", ":", ";"))
        else fragment + "."
        for fragment in unique_fragments
    )
    ozon_payload = {
        "content": [
            {
                "widgetName": "raTextBlock",
                "theme": "default",
                "blocks": [{"text": text}],
            }
        ],
        "version": 0.3,
    }
    return json.dumps(
        ozon_payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _video_template_fields(
    upload_schema: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    for field in upload_schema:
        if not isinstance(field, dict):
            continue
        attribute_id = str(field.get("attribute_id") or "").strip()
        label = str(field.get("attribute_label") or "").strip()
        normalized = normalize_attribute_label(label)
        if not attribute_id or "видео" not in normalized:
            continue
        if "видеооблож" in normalized and any(
            token in normalized for token in ("изображ", "постер", "превью")
        ):
            kind = "video_cover_image_url"
        elif "видеооблож" in normalized and "ссыл" in normalized:
            kind = "video_cover_url"
        elif "ссыл" in normalized:
            kind = "video_url"
        elif "назван" in normalized:
            kind = "video_title"
        else:
            continue
        fields.append(
            {
                "attribute_id": attribute_id,
                "attribute_label": label,
                "kind": kind,
                "is_required": field.get("is_required") is True,
            }
        )
    return fields


def _seller_api_import_item(
    draft_item: dict[str, Any],
    image_urls: list[str],
) -> dict[str, Any]:
    core = draft_item.get("upload_core_fields")
    if not isinstance(core, dict):
        raise ValueError("Confirmed price, dimensions, and weight are required.")
    attributes = [
        _seller_api_attribute(attribute)
        for attribute in draft_item.get("attributes", [])
        if isinstance(attribute, dict)
    ]
    attributes = [attribute for attribute in attributes if attribute is not None]
    title = _draft_attribute_value(draft_item, "title") or str(
        draft_item.get("source_title") or ""
    ).strip()
    offer_id = _draft_attribute_value(draft_item, "seller_code") or _seller_offer_id(
        draft_item
    )
    return {
        "attributes": attributes,
        "barcode": "",
        "complex_attributes": [],
        "currency_code": str(core["currency_code"]),
        "depth": int(
            Decimal(str(core["depth"])).to_integral_value(
                rounding=ROUND_CEILING
            )
        ),
        "description_category_id": int(draft_item["description_category_id"]),
        "dimension_unit": str(core["dimension_unit"]),
        "height": int(
            Decimal(str(core["height"])).to_integral_value(
                rounding=ROUND_CEILING
            )
        ),
        "images": image_urls,
        "name": title,
        "offer_id": offer_id,
        "old_price": str(core["old_price"]),
        "pdf_list": [],
        "premium_price": "",
        "price": str(core["price"]),
        "primary_image": image_urls[0],
        "type_id": int(draft_item["type_id"]),
        "vat": "0",
        "weight": float(core["weight"]),
        "weight_unit": str(core["weight_unit"]),
        "width": int(
            Decimal(str(core["width"])).to_integral_value(
                rounding=ROUND_CEILING
            )
        ),
    }


def _seller_api_attribute(attribute: dict[str, Any]) -> dict[str, Any] | None:
    attribute_id = str(attribute.get("attribute_id") or "").strip()
    value = attribute.get("value")
    if not attribute_id or not _has_content_value(value):
        return None
    dictionary_value_id = attribute.get("dictionary_value_id")
    attribute_type = str(attribute.get("attribute_type") or "").strip().casefold()
    if attribute_type in {"bool", "boolean"}:
        boolean_value = _coerce_boolean_value(value)
        if boolean_value is None:
            raise ValueError(
                f"Boolean Seller API attribute {attribute_id} requires true or false."
            )
        seller_value = {"value": "true" if boolean_value else "false"}
    else:
        seller_value = {"value": str(value)}
    if dictionary_value_id is not None:
        seller_value["dictionary_value_id"] = int(dictionary_value_id)
    return {
        "complex_id": 0,
        "id": int(attribute_id),
        "values": [seller_value],
    }


def _draft_attribute_value(
    draft_item: dict[str, Any],
    canonical_label: str,
) -> str | None:
    for attribute in draft_item.get("attributes", []):
        if not isinstance(attribute, dict):
            continue
        if canonical_attribute_label(attribute.get("label")) != canonical_label:
            continue
        value = str(attribute.get("value") or "").strip()
        if value:
            return value
    return None


def _seller_offer_id(draft_item: dict[str, Any]) -> str:
    supplier_offer_id = re.sub(
        r"[^A-Za-z0-9_-]+",
        "-",
        str(draft_item.get("supplier_offer_id") or "").strip(),
    ).strip("-")
    seed_id = re.sub(
        r"[^A-Za-z0-9_-]+",
        "-",
        str(draft_item.get("seed_id") or "").strip(),
    ).strip("-")
    suffix = supplier_offer_id or seed_id or hashlib.sha256(
        str(draft_item.get("source_title") or "").encode("utf-8")
    ).hexdigest()[:16]
    return f"OZV2-{suffix}"[:50]


def _product_import_status(payload: dict[str, Any]) -> str:
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return "processing"
    statuses = {
        str(item.get("status") or item.get("state") or "").casefold()
        for item in items
        if isinstance(item, dict)
    }
    if any(
        _seller_error_is_blocking(error)
        for item in items
        if isinstance(item, dict)
        for error in (
            item.get("errors")
            if isinstance(item.get("errors"), list)
            else []
        )
        if isinstance(error, dict)
    ):
        return "failed"
    if any(value in {"failed", "error", "declined"} for value in statuses):
        return "failed"
    if statuses and all(
        value in {"imported", "success", "processed", "moderating"}
        for value in statuses
    ):
        return "accepted_by_ozon"
    return "processing"


def _seller_error_is_blocking(error: dict[str, Any]) -> bool:
    level = str(error.get("level") or "").strip().casefold()
    if not level or "warning" in level:
        return False
    return level in {"error", "critical", "fatal"} or level.endswith(
        ("_error", "_critical", "_fatal")
    )


def _product_import_has_status(payload: dict[str, Any], status: str) -> bool:
    expected = str(status or "").strip().casefold()
    items = payload.get("items")
    return bool(
        isinstance(items, list)
        and any(
            isinstance(item, dict)
            and str(item.get("status") or item.get("state") or "")
            .strip()
            .casefold()
            == expected
            for item in items
        )
    )


def _offer_id_from_import_status(payload: dict[str, Any]) -> str:
    items = payload.get("items")
    if not isinstance(items, list):
        return ""
    for item in items:
        if not isinstance(item, dict):
            continue
        offer_id = str(item.get("offer_id") or "").strip()
        if offer_id:
            return offer_id
    return ""


def _reconcile_skipped_import_status(
    payload: dict[str, Any],
    product_state: dict[str, Any],
) -> dict[str, Any]:
    reconciled = json.loads(json.dumps(payload, ensure_ascii=False))
    items = reconciled.get("items")
    if not isinstance(items, list):
        return reconciled
    target_offer_id = str(product_state.get("offer_id") or "").strip()
    errors = (
        product_state.get("errors")
        if isinstance(product_state.get("errors"), list)
        else []
    )
    has_blocking_error = any(
        _seller_error_is_blocking(error)
        for error in errors
        if isinstance(error, dict)
    )
    validation_status = str(
        product_state.get("validation_status") or ""
    ).strip().casefold()
    is_created = product_state.get("is_created") is True
    final_failed = bool(
        has_blocking_error
        or validation_status in {"failed", "error", "declined"}
    )
    for item in items:
        if not isinstance(item, dict):
            continue
        if target_offer_id and str(item.get("offer_id") or "").strip() not in {
            "",
            target_offer_id,
        }:
            continue
        if str(item.get("status") or "").strip().casefold() != "skipped":
            continue
        item["product_id"] = product_state.get("product_id")
        item["sku"] = product_state.get("sku")
        item["is_created"] = product_state.get("is_created")
        item["validation_status"] = product_state.get("validation_status")
        item["errors"] = errors
        if final_failed:
            item["status"] = "failed"
        elif is_created:
            item["status"] = "imported"
    return reconciled


def _product_id_from_import_status(
    payload: dict[str, Any],
    *,
    offer_id: str,
) -> int | None:
    items = payload.get("items")
    if not isinstance(items, list):
        return None
    normalized_offer_id = str(offer_id or "").strip()
    for item in items:
        if not isinstance(item, dict):
            continue
        if (
            normalized_offer_id
            and str(item.get("offer_id") or "").strip()
            != normalized_offer_id
        ):
            continue
        try:
            product_id = int(item.get("product_id"))
        except (TypeError, ValueError):
            continue
        if product_id > 0:
            return product_id
    return None


def _required_not_applicable_fields(
    upload_schema: list[dict[str, Any]],
    generated_field_results: Any,
) -> list[dict[str, str]]:
    if not isinstance(generated_field_results, dict):
        return []
    required_by_key = {
        str(field.get("attribute_id") or ""): field
        for field in upload_schema
        if isinstance(field, dict)
        and field.get("is_required") is True
        and str(field.get("attribute_id") or "")
    }
    conflicts: list[dict[str, str]] = []
    for field_key, field in required_by_key.items():
        result = generated_field_results.get(field_key)
        if not isinstance(result, dict):
            continue
        if (
            str(result.get("decision") or "").strip() == "unresolved"
            and str(result.get("resolution_class") or "").strip()
            == "not_applicable"
        ):
            conflicts.append(
                {
                    "field_key": field_key,
                    "label": str(field.get("attribute_label") or field_key),
                }
            )
    return conflicts


def _effective_upload_schema(
    upload_schema: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    effective: list[dict[str, Any]] = []
    for raw_field in upload_schema:
        if not isinstance(raw_field, dict):
            continue
        field = dict(raw_field)
        normalized_label = normalize_attribute_label(field.get("attribute_label"))
        if normalized_label in _PRE_UPLOAD_COMPLIANCE_DECISION_LABELS:
            field["is_required"] = True
            field["required_reason"] = "ozon_compliance_decision"
            if str(field.get("attribute_type") or "").strip().casefold() in {
                "bool",
                "boolean",
            }:
                field["attribute_type"] = "Boolean"
                field["allowed_values"] = [False, True]
                field["dictionary_id"] = None
        effective.append(field)
    return effective


_PACKAGE_DIMENSION_LABELS = frozenset(
    normalize_attribute_label(label)
    for label in (
        "包装尺寸",
        "包装规格",
        "外包装尺寸",
        "外箱尺寸",
        "包裹尺寸",
        "размер упаковки",
        "размеры упаковки",
        "габариты упаковки",
        "package dimensions",
        "package size",
        "shipping dimensions",
    )
)


def _is_package_dimension_label(normalized_label: str) -> bool:
    if normalized_label in _PACKAGE_DIMENSION_LABELS:
        return True
    return any(
        token in normalized_label
        for token in (
            "包装尺寸",
            "包装规格",
            "外包装尺寸",
            "外箱尺寸",
            "包裹尺寸",
            "размер упаковки",
            "размеры упаковки",
            "габариты упаковки",
            "package dimensions",
            "package size",
            "shipping dimensions",
        )
    )


def _pricing_prefill_from_evidence(
    *,
    candidate: dict[str, Any],
    supplier_product: dict[str, Any],
    supplier_sku: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    values: dict[str, str] = {}
    fields: dict[str, dict[str, str]] = {}

    def add(
        field_key: str,
        value: str | None,
        *,
        source: str,
        label: str,
    ) -> None:
        if value is None or field_key in values:
            return
        values[field_key] = value
        fields[field_key] = {
            "source": source,
            "label": label,
            "confidence": "high",
        }

    price = supplier_sku.get("price")
    if isinstance(price, dict):
        currency = str(price.get("currency") or "CNY").strip().upper()
        if currency == "CNY":
            add(
                "purchase_price_cny",
                _pricing_decimal_text(price.get("amount"), preserve_scale=True),
                source="locked_supplier_sku.price",
                label="已锁定 1688 SKU 价格",
            )

    shipping = supplier_product.get("domestic_shipping_evidence")
    if isinstance(shipping, dict):
        shipping_value = _pricing_decimal_text(shipping.get("fee"))
        if shipping_value is None and (
            shipping.get("free_shipping_visible") is True
            or shipping.get("free_shipping") is True
        ):
            shipping_value = "0"
        add(
            "domestic_shipping_cny",
            shipping_value,
            source="supplier.domestic_shipping_evidence",
            label="1688 页面国内运费",
        )

    supplier_attributes = supplier_product.get("attributes")
    ozon_attributes = candidate.get("attributes")
    for attributes, source_prefix, source_label in (
        (
            supplier_attributes,
            "supplier.attributes",
            "已锁定 1688 SKU 商品属性",
        ),
        (
            ozon_attributes,
            "ozon.attributes",
            "Ozon 原商品包装属性",
        ),
    ):
        if not isinstance(attributes, dict):
            continue
        package_values = _package_measurements(attributes)
        add(
            "package_weight_g",
            package_values.get("package_weight_g"),
            source=f"{source_prefix}.package_weight",
            label=source_label,
        )
        for key in (
            "package_length_cm",
            "package_width_cm",
            "package_height_cm",
        ):
            add(
                key,
                package_values.get(key),
                source=f"{source_prefix}.package_dimensions",
                label=source_label,
            )

    return {"values": values, "fields": fields}


def _package_measurements(attributes: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    separate_dimensions: dict[str, str] = {}
    for raw_label, raw_value in attributes.items():
        normalized_label = normalize_attribute_label(raw_label)
        canonical_label = canonical_attribute_label(raw_label)
        if canonical_label == "package_weight":
            weight = _pricing_weight_grams(raw_value, raw_label)
            if weight is not None:
                result["package_weight_g"] = weight
            continue
        if _is_package_dimension_label(normalized_label):
            dimensions = _pricing_dimensions_cm(raw_value, raw_label)
            if dimensions is not None:
                result.update(
                    {
                        "package_length_cm": dimensions[0],
                        "package_width_cm": dimensions[1],
                        "package_height_cm": dimensions[2],
                    }
                )
            continue
        dimension_key = {
            "package_length": "package_length_cm",
            "package_width": "package_width_cm",
            "package_height": "package_height_cm",
        }.get(canonical_label)
        if dimension_key:
            dimension = _pricing_length_cm(raw_value, raw_label)
            if dimension is not None:
                separate_dimensions[dimension_key] = dimension
    for key, value in separate_dimensions.items():
        result.setdefault(key, value)
    return result


def _pricing_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            decimal_value = Decimal(str(value))
        except Exception:
            return None
        return decimal_value if decimal_value.is_finite() else None
    text = str(value).strip().replace("\u00a0", " ")
    match = re.search(r"[-+]?\d+(?:[.,]\d+)?", text)
    if not match:
        return None
    try:
        decimal_value = Decimal(match.group(0).replace(",", "."))
    except Exception:
        return None
    return decimal_value if decimal_value.is_finite() else None


def _pricing_decimal_text(
    value: Any,
    *,
    preserve_scale: bool = False,
) -> str | None:
    decimal_value = _pricing_decimal(value)
    if decimal_value is None or decimal_value < 0:
        return None
    if preserve_scale and isinstance(value, str):
        stripped = value.strip().replace(",", ".")
        if re.fullmatch(r"\d+(?:\.\d+)?", stripped):
            return stripped
    return format(decimal_value.normalize(), "f")


def _pricing_measurement_unit(value: Any, label: Any) -> str | None:
    text = f"{label} {value}".casefold().replace("ё", "е")
    if re.search(r"(?:^|[^\w])(кг|kg)(?:$|[^\w])", text) or any(
        token in text for token in ("千克", "公斤")
    ):
        return "kg"
    if re.search(r"(?:^|[^\w])(мм|mm)(?:$|[^\w])", text) or "毫米" in text:
        return "mm"
    if re.search(r"(?:^|[^\w])(см|cm)(?:$|[^\w])", text) or "厘米" in text:
        return "cm"
    if re.search(r"(?:^|[^\w])(м|m)(?:$|[^\w])", text) or "米" in text:
        return "m"
    if re.search(r"(?:^|[^\w])(гр|г|g)(?:$|[^\w])", text) or "克" in text:
        return "g"
    return None


def _pricing_weight_grams(value: Any, label: Any) -> str | None:
    decimal_value = _pricing_decimal(value)
    unit = _pricing_measurement_unit(value, label)
    if decimal_value is None or decimal_value <= 0 or unit not in {"g", "kg"}:
        return None
    if unit == "kg":
        decimal_value *= Decimal("1000")
    return format(decimal_value.normalize(), "f")


def _pricing_length_cm(value: Any, label: Any) -> str | None:
    decimal_value = _pricing_decimal(value)
    unit = _pricing_measurement_unit(value, label)
    if decimal_value is None or decimal_value <= 0 or unit not in {"mm", "cm", "m"}:
        return None
    factor = {"mm": Decimal("0.1"), "cm": Decimal("1"), "m": Decimal("100")}[unit]
    return format((decimal_value * factor).normalize(), "f")


def _pricing_dimensions_cm(
    value: Any,
    label: Any,
) -> tuple[str, str, str] | None:
    text = str(value or "").replace(",", ".")
    numbers = re.findall(r"\d+(?:\.\d+)?", text)
    unit = _pricing_measurement_unit(value, label)
    if len(numbers) < 3 or unit not in {"mm", "cm", "m"}:
        return None
    factor = {"mm": Decimal("0.1"), "cm": Decimal("1"), "m": Decimal("100")}[unit]
    dimensions = [Decimal(number) * factor for number in numbers[:3]]
    if any(dimension <= 0 for dimension in dimensions):
        return None
    return tuple(format(dimension.normalize(), "f") for dimension in dimensions)  # type: ignore[return-value]


def _pricing_upload_core_fields(
    pricing_record: dict[str, Any],
    *,
    currency_code: str = "RUB",
) -> dict[str, str] | None:
    inputs = pricing_record.get("inputs")
    calculation = pricing_record.get("calculation")
    if not isinstance(inputs, dict) or not isinstance(calculation, dict):
        return None
    required_inputs = (
        "package_length_cm",
        "package_width_cm",
        "package_height_cm",
        "package_weight_g",
    )
    if any(inputs.get(key) in (None, "") for key in required_inputs):
        return None
    normalized_currency = str(currency_code or "").strip().upper()
    if normalized_currency == "CNY":
        price_value = calculation.get("listing_price_cny")
        old_price_value = calculation.get("old_price_cny")
        if old_price_value in (None, "") and price_value not in (None, ""):
            snapshot = pricing_record.get("parameter_snapshot")
            discount_rate = (
                snapshot.get("old_price_discount_rate")
                if isinstance(snapshot, dict)
                else None
            )
            if discount_rate not in (None, ""):
                old_price_value = round_up_to_dot_90(
                    Decimal(str(price_value)) / Decimal(str(discount_rate))
                )
    elif normalized_currency == "RUB":
        price_value = calculation.get("listing_price_rub")
        old_price_value = calculation.get("old_price_rub")
    else:
        return None
    if price_value in (None, "") or old_price_value in (None, ""):
        return None

    def normalized(value: Any) -> str:
        return format(Decimal(str(value)).normalize(), "f")

    def millimeters(key: str) -> str:
        return normalized(
            (Decimal(str(inputs[key])) * Decimal("10")).to_integral_value(
                rounding=ROUND_CEILING
            )
        )

    return {
        "price": normalized(price_value),
        "old_price": normalized(old_price_value),
        "currency_code": normalized_currency,
        "depth": millimeters("package_length_cm"),
        "width": millimeters("package_width_cm"),
        "height": millimeters("package_height_cm"),
        "dimension_unit": "mm",
        "weight": normalized(inputs["package_weight_g"]),
        "weight_unit": "g",
    }


def _pricing_record_validation_errors(
    pricing_record: dict[str, Any],
) -> list[str]:
    inputs = pricing_record.get("inputs")
    if not isinstance(inputs, dict):
        return ["Confirmed pricing evidence has no input snapshot."]
    required_keys = {
        "purchase_price_cny",
        "domestic_shipping_cny",
        "package_weight_g",
        "package_length_cm",
        "package_width_cm",
        "package_height_cm",
        "target_margin_rate",
    }
    missing_keys = sorted(
        key for key in required_keys if inputs.get(key) in (None, "")
    )
    if missing_keys:
        return [
            "Confirmed pricing evidence is missing inputs: "
            + ", ".join(missing_keys)
        ]
    try:
        PricingInput.from_values(
            purchase_price_cny=inputs["purchase_price_cny"],
            domestic_shipping_cny=inputs["domestic_shipping_cny"],
            package_weight_g=inputs["package_weight_g"],
            package_length_cm=inputs["package_length_cm"],
            package_width_cm=inputs["package_width_cm"],
            package_height_cm=inputs["package_height_cm"],
            target_margin_rate=inputs["target_margin_rate"],
        )
    except (TypeError, ValueError) as exc:
        return [str(exc)]
    return []


def _supplier_product_url(
    supplier_product: dict[str, Any],
    supplier_offer_id: str,
) -> str | None:
    for key in ("supplier_url", "final_url", "url"):
        value = str(supplier_product.get(key) or "").strip()
        if value:
            return value
    if supplier_offer_id:
        return (
            "https://detail.1688.com/offer/"
            f"{supplier_offer_id}.html"
        )
    return None
