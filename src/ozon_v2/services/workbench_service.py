from __future__ import annotations

import json
import random
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

from ozon_v2.adapters.fs_repo import FsRepo
from ozon_v2.adapters.seller_api import SellerApiAdapter, SellerApiError
from ozon_v2.app.result import Result
from ozon_v2.domain.models import QueryGenerationStatus, SeedProduct, SeedSearchQuery, WorkbenchAction, WorkbenchState, utc_now_iso
from ozon_v2.domain.policies import decide_seed_existing_product_dedupe, seed_has_generated_ozon_query
from ozon_v2.domain.supplier_sku import SupplierSkuOption, SupplierSkuSelectionReceipt, validate_supplier_sku_option
from ozon_v2.domain.state_machine import allowed_workbench_actions, transition_workbench_state
from ozon_v2.domain.validators import validate_attribute_template_result, validate_ozon_collection_result, validate_seed_ready_for_ozon
from ozon_v2.images.contracts import SubjectMasterSelection
from ozon_v2.images.queue import ImageGenerationQueue, ImageRepairRequestError
from ozon_v2.services.collection_contract_service import (
    CREATIVE_FIELDS_REQUIRING_REWRITE,
    CollectionContractService,
    OBJECTIVE_ATTRIBUTE_PREFILL_FIELDS,
)
from ozon_v2.services.credential_service import CredentialService
from ozon_v2.services.seed_query_service import SeedQueryService
from ozon_v2.services.seller_history_service import SellerHistoryService


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
        cleared = self.repo.clear_workbench_batches()
        bridge = self.repo.clear_browser_bridge_task_status()
        return Result.success(
            "workbench.batches_cleared",
            "All current and historical workbench batches were cleared.",
            {
                **cleared,
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
        run = self.repo.load_run(run_id)
        if WorkbenchState(run["status"]) == WorkbenchState.OZON_COLLECTED:
            try:
                ozon_result = self.repo.load_ozon_collection_result(run_id)
            except FileNotFoundError:
                ozon_result = None
            if ozon_result is not None:
                review = self._build_supplier_review(run_id, ozon_result)
                review_path = self.repo.save_supplier_review(run_id, review)
                run["supplier_review_path"] = str(review_path)
                run["status"] = transition_workbench_state(
                    WorkbenchState(run["status"]), WorkbenchAction.OPEN_SUPPLIER_REVIEW
                ).value
                self.repo.save_run(run)
                self.repo.append_run_event(
                    run_id,
                    "supplier_review.opened",
                    "Existing Ozon collection was moved to the supplier review stage.",
                    {"review_path": str(review_path), "item_count": len(review.get("items", []))},
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

        used_ids = self.repo.load_used_seed_ids()
        rejected_ids = {item.get("seed_id") for item in self.repo.load_rejected_seed_attempts(run_id)}
        sampled_ids = {seed.seed_id for seed in sampled}
        excluded_ids = used_ids | rejected_ids | sampled_ids
        existing_products = self.repo.load_existing_products()
        eligible: list[SeedProduct] = []
        for seed in self.repo.load_active_seeds():
            if seed.seed_id in excluded_ids:
                continue
            decision = decide_seed_existing_product_dedupe(seed, existing_products)
            if decision.kind.value == "clear":
                eligible.append(seed)
        if not eligible:
            return Result.failure(
                "workbench.exhausted_seed_no_replacement",
                "No eligible replacement seed remains after store dedupe filtering.",
                data={"run_id": run_id, "rejected_seed_id": rejected_seed_id},
            )

        selected_random_seed = random_seed if random_seed is not None else random.SystemRandom().randint(1, 2**31 - 1)
        replacement = self.repo.sample_seeds(eligible, 1, selected_random_seed)[0]
        replacement_index = next(index for index, seed in enumerate(sampled) if seed.seed_id == rejected_seed_id)
        sampled[replacement_index] = replacement
        self.repo.save_sampled_seeds(run_id, sampled)
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
        seeds = self._safe_load_sampled_seeds(run_id)
        all_seed_ids = [seed.seed_id for seed in seeds]
        expected_seed_ids = self._pending_or_all_seed_ids(
            run,
            all_seed_ids,
            result_kind="attribute_template",
        )
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
        if run.get("replacement_pending_seed_ids"):
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
        self.repo.save_run(run)
        event = self.repo.append_run_event(
            run_id,
            "attribute_template.ingested",
            "Attribute template result was ingested and the template gate is complete.",
            {"result_path": str(result_path), "seed_count": len(expected_seed_ids)},
        )
        return Result.success(
            "workbench.attribute_template_ingested",
            "Attribute template result was ingested and the template gate is complete.",
            self._response_payload(run, event),
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

    def _seller_attribute_template_errors(self, run_id: str, expected_seed_ids: list[str]) -> list[str]:
        try:
            payload = self.repo.load_attribute_template_result(run_id)
        except FileNotFoundError:
            return ["Seller category attribute template result is missing."]
        return validate_attribute_template_result(payload, expected_seed_ids, require_seller_schema=True)

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
        run = self.repo.load_run(run_id)
        status = WorkbenchState(run["status"])
        task_type_by_status = {
            WorkbenchState.OZON_COLLECTING: "ozon_collection",
            WorkbenchState.SUPPLIER_REVIEW: "supplier_selection",
            WorkbenchState.SUPPLIER_COLLECTING: "supplier_collection",
        }
        task_type = task_type_by_status.get(status)
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
                total_count = len(review_seed_ids)
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
                completed_count = len(review_seed_ids.intersection(captured_seed_ids))
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
        seeds = self._safe_load_sampled_seeds(run_id)
        all_seed_ids = [seed.seed_id for seed in seeds]
        expected_seed_ids = self._pending_or_all_seed_ids(
            run,
            all_seed_ids,
            result_kind="ozon_collection",
        )
        seller_template_errors = self._seller_attribute_template_errors(run_id, all_seed_ids)
        if seller_template_errors:
            event = self.repo.append_run_event(
                run_id,
                "ozon_collection.blocked_invalid_seller_template",
                "Ozon collection result was rejected because the Seller category attribute template is missing or invalid.",
                {"errors": seller_template_errors},
            )
            return Result.failure(
                "workbench.seller_attribute_template_required",
                "Seller category attribute template must be valid before accepting Ozon collection.",
                errors=seller_template_errors,
                data=self._response_payload(run, event),
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
        if run.get("replacement_pending_seed_ids"):
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
        result_path = self.repo.save_ozon_collection_result(run_id, payload)
        current = WorkbenchState(run["status"])
        run["status"] = transition_workbench_state(current, WorkbenchAction.MARK_OZON_COLLECTED).value
        run["ozon_collected"] = True
        run["ozon_collection_result_path"] = str(result_path)
        run.pop("replacement_pending_seed_ids", None)
        review = self._build_supplier_review(run_id, payload)
        review_path = self.repo.save_supplier_review(run_id, review)
        run["supplier_review_path"] = str(review_path)
        run["status"] = transition_workbench_state(
            WorkbenchState(run["status"]), WorkbenchAction.OPEN_SUPPLIER_REVIEW
        ).value
        self.repo.save_run(run)
        event = self.repo.append_run_event(
            run_id,
            "ozon_collection.ingested",
            "Ozon collection result was ingested and the Ozon gate is complete.",
            {"result_path": str(result_path), "candidate_count": len(payload.get("ozon_candidates", []))},
        )
        return Result.success(
            "workbench.ozon_collection_ingested",
            "Ozon collection result was ingested and the Ozon gate is complete.",
            self._response_payload(run, event),
        )

    def supplier_review(self, run_id: str) -> Result:
        run = self.repo.load_run(run_id)
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
            ozon_images = media.get("selected_sku_images") or media.get("main_gallery_images") or []
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
        all_selected = bool(items) and all(item["supplier_sku_selection"] for item in items)
        all_subjects = bool(items) and all(item["subject_master"] for item in items)
        all_jobs = bool(items) and all(item["image_job"] for item in items)
        all_reviewable = all_jobs and all(
            item["image_job"].get("status") == "manual_review_required" for item in items
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
        elif not all_reviewable:
            gate_code = "waiting_for_codex_workers"
            gate_message = "任务已进入本地队列，等待最多 5 个动态 Codex 生图子智能体按可用容量处理 (Waiting for available Codex image subagents)."
        else:
            gate_code = "image_review_required"
            gate_message = "八张图片已回写，等待用户逐张审核 (Eight images are ready for review)."

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
                "image_gate": {
                    "ready": all_reviewable,
                    "code": gate_code,
                    "message": gate_message,
                },
            },
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
        candidates = ozon_result.get("ozon_candidates") if isinstance(ozon_result.get("ozon_candidates"), list) else []
        items: list[dict[str, Any]] = []
        for candidate in candidates:
            seed_id = str(candidate.get("seed_id") or "")
            template = templates_by_seed.get(seed_id, {})
            schema = template.get("upload_attribute_schema") if isinstance(template.get("upload_attribute_schema"), list) else []
            prefill_plan = template.get("draft_prefill_plan") if isinstance(template.get("draft_prefill_plan"), list) else []
            seller_template = template.get("seller_attribute_template") or {}
            category_candidates = template.get("category_candidates") if isinstance(template.get("category_candidates"), list) else []
            media = candidate.get("selected_sku_media") or {}
            images = media.get("selected_sku_images") or media.get("main_gallery_images") or []
            items.append(
                {
                    "seed_id": seed_id,
                    "source_title": candidate.get("title"),
                    "source_description": (candidate.get("content_score_evidence") or {}).get("description_or_rich_content_blocks") or [],
                    "source_attributes": candidate.get("attributes") or {},
                    "source_image": images[0] if images else None,
                    "selected_options": (candidate.get("target_sku") or {}).get("selected_options") or {},
                    "category_path": seller_template.get("matched_category_path") or (category_candidates[0].get("category_path") if category_candidates else None),
                    "description_category_id": seller_template.get("description_category_id"),
                    "type_id": seller_template.get("type_id"),
                    "required_attribute_count": sum(1 for field in schema if field.get("is_required") is True),
                    "attribute_schema_count": len(schema),
                    "prefill_plan_count": len(prefill_plan),
                    "prefill_plan": prefill_plan,
                    "template_ready": bool(schema and seller_template),
                    "generated_content_ready": False,
                    "generated_images_ready": False,
                }
            )

        template_ready = bool(items) and all(item["template_ready"] for item in items)
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
                    "generated_content_ready": False,
                    "images_ready": False,
                    "draft_ready": draft_ready,
                    "publish_locked": bool(run.get("publish_locked", True)),
                    "ready_to_build": False,
                },
            },
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

    def capture_supplier_selection_product(self, run_id: str, payload: dict[str, Any]) -> Result:
        run = self.repo.load_run(run_id)
        if WorkbenchState(run["status"]) != WorkbenchState.SUPPLIER_REVIEW:
            return Result.failure(
                "supplier_selection.not_expected",
                "A managed 1688 product can only be captured during supplier review.",
                data={"run_id": run_id, "status": run["status"]},
            )
        review = self.repo.load_supplier_review(run_id)
        items = [item for item in review.get("items", []) if isinstance(item, dict)]
        seed_id = str(payload.get("seed_id") or "").strip()
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
        product["seed_id"] = seed_id
        product["supplier_url"] = supplier_url

        draft_path = self.repo.run_dir(run_id) / "supplier_selection_draft.json"
        draft = self.repo.load_supplier_selection_draft(run_id) if draft_path.exists() else {
            "schema_version": 1,
            "run_id": run_id,
            "supplier_products": [],
            "created_at": utc_now_iso(),
        }
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
        if WorkbenchState(run["status"]) != WorkbenchState.SUPPLIER_REVIEW:
            return Result.failure(
                "supplier_selection.reset_not_allowed",
                "A captured supplier can only be reset during supplier review.",
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

        excluded_ids = (
            self.repo.load_used_seed_ids()
            | self.repo.load_blacklisted_seed_ids()
            | {seed.seed_id for seed in sampled}
            | {str(item.get("seed_id")) for item in self.repo.load_rejected_seed_attempts(run_id) if item.get("seed_id")}
        )
        existing_products = self.repo.load_existing_products()
        eligible = [
            seed
            for seed in self.repo.load_active_seeds()
            if seed.seed_id not in excluded_ids
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
        self.repo.remove_active_seeds({seed_id})
        self.repo.append_rejected_seed_attempt(run_id, rejected_seed, reason, replacement.seed_id)

        sampled[next(index for index, seed in enumerate(sampled) if seed.seed_id == seed_id)] = replacement
        self.repo.save_sampled_seeds(run_id, sampled)
        template_payload["seed_templates"] = [
            item for item in template_payload.get("seed_templates", []) if str(item.get("seed_id") or "") != seed_id
        ]
        self.repo.save_attribute_template_result(run_id, template_payload)
        ozon_payload["ozon_candidates"] = [
            item for item in ozon_payload.get("ozon_candidates", []) if str(item.get("seed_id") or "") != seed_id
        ]
        self.repo.save_ozon_collection_result(run_id, ozon_payload)
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
            "price",
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
                "price": product.get("price"),
                "domestic_shipping_evidence": product.get("domestic_shipping_evidence"),
            }
            if not allow_deferred_sku:
                required["sku_options"] = product.get("sku_options")
            for field, value in required.items():
                if value in (None, "", [], {}):
                    errors.append(f"{seed_id}: required public field is missing: {field}.")

            raw_sku_options = product.get("sku_options") if isinstance(product.get("sku_options"), list) else []
            valid_sku_options: list[dict[str, Any]] = []
            sku_option_candidates: list[dict[str, Any]] = []
            seen_supplier_sku_ids: set[str] = set()
            for index, raw_option in enumerate(raw_sku_options):
                if not isinstance(raw_option, dict):
                    option_errors = ["must be an object"]
                else:
                    try:
                        option = SupplierSkuOption.from_dict(raw_option)
                    except (TypeError, ValueError) as exc:
                        option_errors = [f"could not be parsed: {exc}"]
                    else:
                        option_errors = validate_supplier_sku_option(option)
                        if option.supplier_sku_id in seen_supplier_sku_ids:
                            option_errors = [*option_errors, f"duplicate supplier_sku_id: {option.supplier_sku_id}"]
                        if not option_errors:
                            seen_supplier_sku_ids.add(option.supplier_sku_id)
                            valid_sku_options.append(option.to_dict())
                            continue
                if allow_deferred_sku:
                    candidate = dict(raw_option) if isinstance(raw_option, dict) else {"raw_value": raw_option}
                    candidate["validation_errors"] = option_errors
                    sku_option_candidates.append(candidate)
                else:
                    for error in option_errors:
                        errors.append(f"{seed_id}: sku_options[{index}] {error}.")

            product["sku_options"] = valid_sku_options
            if sku_option_candidates:
                product["sku_option_candidates"] = sku_option_candidates
            sku_matrix_complete = bool(valid_sku_options) and not sku_option_candidates
            product["sku_matrix_status"] = (
                "complete" if sku_matrix_complete else "manual_confirmation_required"
            )
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

        subject_path = self.repo.run_dir(run_id) / "subject_masters.json"
        subjects = self.repo.load_subject_masters(run_id) if subject_path.exists() else {
            "schema_version": 1,
            "run_id": run_id,
            "items": {},
        }
        subject_entry = subjects.get("items", {}).get(seed_id)
        job_id = str(subject_entry.get("image_job_id") or "") if isinstance(subject_entry, dict) else ""
        if isinstance(subject_entry, dict):
            queue = self._image_generation_queue()
            if not job_id or not queue.stop_unstarted(job_id):
                job = queue.get_job(job_id) if job_id else None
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
        allowed_urls = set(receipt.supplier_sku.image_urls)
        allowed_urls.update(self._supplier_product_images(supplier_product))
        foreign_urls = [url for url in selected_urls if url not in allowed_urls]
        if foreign_urls:
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

        queue = self._image_generation_queue()
        job = queue.enqueue(receipt=receipt, subject_master=subject_master)
        stored.setdefault("items", {})[seed_id] = {
            "subject_master": subject_master.to_dict(),
            "image_job_id": job["job_id"],
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

        image_job = self._image_job_payload(str(job["job_id"]))
        event = self.repo.append_run_event(
            run_id,
            "subject_master.confirmed",
            "The exact supplier SKU subject evidence was locked and queued for image generation.",
            {
                "seed_id": seed_id,
                "supplier_sku_id": receipt.supplier_sku_id,
                "set_quantity": receipt.supplier_sku.set_quantity,
                "subject_master_sha256": subject_master.subject_master_sha256,
                "image_job_id": job["job_id"],
                "image_stage_started": all_confirmed,
            },
        )
        return Result.success(
            "subject_master.confirmed",
            "The exact supplier subject evidence was locked and queued.",
            self._response_payload(
                run,
                event,
                {
                    "subject_master": subject_master.to_dict(),
                    "image_job": image_job,
                    "all_subject_masters_confirmed": all_confirmed,
                },
            ),
        )

    def stop_image_job(self, run_id: str, job_id: str) -> Result:
        queue = self._image_generation_queue()
        job = queue.get_job(job_id)
        if job is None or str(job.get("run_id") or "") != run_id:
            return Result.failure("image_job.not_found", "The image job was not found in this batch.")
        queue.stop(job_id)
        image_job = self._image_job_payload(job_id)
        event = self.repo.append_run_event(
            run_id,
            "image_job.stopped",
            "Image generation was stopped by the user.",
            {"image_job_id": job_id},
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
            run = self.repo.load_run(run_id)
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
                if not run.get("attribute_template_contract_ready"):
                    result = self.dispatch(run_id, WorkbenchAction.START_ATTRIBUTE_TEMPLATE_COLLECTION.value)
                    history.append(self._history_item(result))
                    if not result.ok:
                        return self._autopilot_blocked(run_id, result.code, result.message, history)
                    continue
                return self._autopilot_blocked(
                    run_id,
                    "attribute_template_worker_required",
                    "Ozon attribute template contract is ready; an approved browser worker must collect and ingest the template.",
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
                result = self.dispatch(run_id, WorkbenchAction.START_OZON_COLLECTION.value)
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
                    "image_processing_worker_required",
                    "Supplier collection is complete; image processing is the next independent stage.",
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
        return [action.value for action in actions]

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
        existing_products = self.repo.load_existing_products()
        eligible: list[SeedProduct] = []
        blocked: list[dict[str, Any]] = []
        for seed in self.repo.load_active_seeds():
            if seed.seed_id in blacklisted_seed_ids:
                blocked.append({"seed_id": seed.seed_id, "reason": "blacklisted"})
                continue
            if seed.seed_id in used_seed_ids:
                blocked.append({"seed_id": seed.seed_id, "reason": "already used"})
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
        current = WorkbenchState(run["status"])
        run["status"] = transition_workbench_state(current, WorkbenchAction.SELECT_SEEDS).value
        run["random_seed"] = random_seed
        run["sampled_seed_ids"] = [seed.seed_id for seed in sampled]
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
        if not run.get("attribute_template_collected"):
            event = self.repo.append_run_event(
                run["run_id"],
                "ozon_collection.blocked_missing_attribute_template",
                "Ozon collection was blocked because seed attribute templates are not collected.",
                {"attribute_template_collected": False},
            )
            return Result.failure(
                "workbench.attribute_template_missing",
                "Seed attribute templates must be collected before Ozon product collection.",
                data=self._response_payload(run, event),
            )
        seeds = self._safe_load_sampled_seeds(run["run_id"])
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
        seller_template_errors = self._seller_attribute_template_errors(run["run_id"], [seed.seed_id for seed in seeds])
        if seller_template_errors:
            event = self.repo.append_run_event(
                run["run_id"],
                "ozon_collection.blocked_invalid_seller_template",
                "Ozon collection was blocked because Seller category attribute template is missing or invalid.",
                {"errors": seller_template_errors},
            )
            return Result.failure(
                "workbench.seller_attribute_template_required",
                "Seller category attribute template must be valid before Ozon collection.",
                errors=seller_template_errors,
                data=self._response_payload(run, event),
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

    def _ozon_collection_progress(self, run: dict, seeds: list[SeedProduct]) -> dict[str, int]:
        total = len(seeds)
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

        final_path = self.repo.run_dir(run["run_id"]) / "ozon_collection_result.json"
        if final_path.exists():
            success = len(self.repo.load_ozon_collection_result(run["run_id"]).get("ozon_candidates", []))
        else:
            try:
                success = int(live.get("success_count", 0) or 0)
            except (TypeError, ValueError):
                success = 0
        success = min(total, max(0, success))
        failure = min(len(failed), max(total - success, 0))
        processed = min(total, success + failure)
        return {
            "total_count": total,
            "processed_count": processed,
            "success_count": success,
            "failure_count": failure,
            "replacement_count": len(replaced),
            "pending_count": max(total - processed, 0),
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
        return {
            **snapshot["job"],
            "slots": snapshot["slots"],
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

    def _build_supplier_review(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
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
            images = media.get("selected_sku_images") or media.get("main_gallery_images") or []
            existing = existing_by_seed.get(str(candidate.get("seed_id") or ""), {})
            items.append(
                {
                    "seed_id": candidate.get("seed_id"),
                    "ozon_product_id": candidate.get("ozon_product_id"),
                    "ozon_title": candidate.get("title"),
                    "ozon_url": candidate.get("ozon_url"),
                    "ozon_main_image": images[0] if images else None,
                    "selected_options": selected_options,
                    "dimension_evidence": self._dimension_evidence(selected_options, candidate.get("attributes") or {}),
                    "key_attributes": candidate.get("attributes") or {},
                    "supplier_url": existing.get("supplier_url"),
                    "user_verified_exact_match": existing.get("user_verified_exact_match") is True,
                    "verified_at": existing.get("verified_at"),
                }
            )
        return {"run_id": run_id, "items": items, "created_at": utc_now_iso(), "updated_at": utc_now_iso()}

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
            merged = dict(stored_item)
            merged["ozon_product"] = ozon_product
            merged["supplier_product"] = supplier_product
            merged["supplier_sku_options"] = supplier_sku_options
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
                    "price": supplier.get("price"),
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

    @staticmethod
    def _sku_measurement_tokens(value: Any) -> list[str]:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True) if not isinstance(value, str) else value
        normalized = text.translate(str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")).lower()
        unit_aliases = {
            "мл": "ml",
            "ml": "ml",
            "мм": "mm",
            "mm": "mm",
            "см": "cm",
            "cm": "cm",
            "кг": "kg",
            "kg": "kg",
            "л": "l",
            "l": "l",
            "г": "g",
            "g": "g",
            "м": "m",
            "m": "m",
        }
        units = "мл|ml|мм|mm|см|cm|кг|kg|л|l|г|g|м|m"
        tokens: set[str] = set()

        def number_text(raw: str) -> str:
            number = float(raw.replace(",", "."))
            return str(int(number)) if number.is_integer() else str(number)

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
        return {
            "target_measurements": target_measurements,
            "matching_sku_ids": matching_sku_ids,
            "other_sku_ids": other_sku_ids,
            "recommended_sku_id": recommended_sku_id,
            "candidates": candidate_decisions,
        }

    def _supplier_sku_options(self, product: dict[str, Any]) -> list[dict[str, Any]]:
        raw_options = product.get("sku_options")
        if isinstance(raw_options, list) and raw_options:
            return [dict(option) for option in raw_options if isinstance(option, dict)]
        sku_groups = product.get("sku_groups")
        if isinstance(sku_groups, list) and sku_groups:
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
        if not offer_id or not amount or not images:
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
