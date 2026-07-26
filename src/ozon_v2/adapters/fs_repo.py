from __future__ import annotations

import csv
import json
import random
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from ozon_v2.app.context import AppContext, build_default_context
from ozon_v2.domain.credentials import CredentialStatus, SellerCredentials
from ozon_v2.domain.pricing import PricingPolicy
from ozon_v2.domain.models import (
    CollectionPair,
    ExistingStoreProduct,
    RunEvent,
    RunStatus,
    SeedProduct,
    WorkbenchState,
    to_plain,
    utc_now_iso,
)


_JSON_WRITE_LOCK = threading.RLock()


class FsRepo:
    def __init__(self, context: AppContext | None = None) -> None:
        self.context = context or build_default_context()
        self.runtime_root = self.context.runtime_root

    @property
    def config_dir(self) -> Path:
        return self.runtime_root / "config"

    @property
    def state_dir(self) -> Path:
        return self.runtime_root / "state"

    @property
    def runs_dir(self) -> Path:
        return self.runtime_root / "runs"

    @property
    def active_seed_path(self) -> Path:
        return self.state_dir / "seed_pool.active.json"

    @property
    def used_seed_path(self) -> Path:
        return self.state_dir / "seed_pool.used.jsonl"

    @property
    def seed_blacklist_path(self) -> Path:
        return self.state_dir / "seed_pool.blacklist.jsonl"

    @property
    def existing_store_dedupe_path(self) -> Path:
        return self.state_dir / "existing_store_dedupe.jsonl"

    @property
    def existing_store_dedupe_meta_path(self) -> Path:
        return self.state_dir / "existing_store_dedupe.meta.json"

    @property
    def config_initial_seed_path(self) -> Path:
        return self.config_dir / "seed_pool.initial.json"

    @property
    def credentials_path(self) -> Path:
        return self.config_dir / "seller_credentials.local.json"

    @property
    def credentials_template_path(self) -> Path:
        return self.config_dir / "seller_credentials.template.json"

    @property
    def pricing_settings_path(self) -> Path:
        return self.config_dir / "pricing_settings.json"

    @property
    def public_media_settings_path(self) -> Path:
        return self.config_dir / "public_media_settings.json"

    @property
    def credential_assistant_state_path(self) -> Path:
        return self.state_dir / "credential_assistant_state.json"

    @property
    def browser_bridge_status_path(self) -> Path:
        return self.state_dir / "browser_bridge.status.json"

    def initialize_runtime(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        if not self.config_initial_seed_path.exists():
            shutil.copy2(self.context.paths.initial_seed_json, self.config_initial_seed_path)
        if not self.active_seed_path.exists():
            package = self._read_json(self.context.paths.initial_seed_json)
            self._write_json(
                self.active_seed_path,
                {
                    "package_version": package["package_version"],
                    "initialized_at": utc_now_iso(),
                    "seeds": package["seeds"],
                },
            )
        self.used_seed_path.touch(exist_ok=True)
        self.seed_blacklist_path.touch(exist_ok=True)
        self.existing_store_dedupe_path.touch(exist_ok=True)
        if not self.credentials_template_path.exists():
            self.write_credentials_template()

    def write_credentials_template(self) -> Path:
        template = {
            "client_id": "填入店铺ID或Seller API Client-Id",
            "api_key": "填入Seller API Key",
            "notes": "Copy this file to seller_credentials.local.json or use ozon_v2_save_credentials. Never commit real keys.",
        }
        self._write_json(self.credentials_template_path, template)
        return self.credentials_template_path

    def save_credentials(self, credentials: SellerCredentials) -> None:
        self.initialize_runtime()
        existing = self.load_credentials()
        if existing:
            credentials.created_at = existing.created_at
        credentials.updated_at = utc_now_iso()
        self._write_json(self.credentials_path, credentials.to_private_dict())

    def load_credentials(self) -> SellerCredentials | None:
        if not self.credentials_path.exists():
            return None
        return SellerCredentials.from_dict(self._read_json(self.credentials_path))

    def credential_status(self) -> CredentialStatus:
        self.initialize_runtime()
        credentials = self.load_credentials()
        if not credentials:
            return CredentialStatus(
                configured=False,
                credentials_path=str(self.credentials_path),
                template_path=str(self.credentials_template_path),
            )
        safe = credentials.to_safe_dict()
        return CredentialStatus(
            configured=True,
            credentials_path=str(self.credentials_path),
            template_path=str(self.credentials_template_path),
            client_id=safe["client_id"],
            api_key_masked=safe["api_key_masked"],
        )

    def load_active_seeds(self) -> list[SeedProduct]:
        self.initialize_runtime()
        payload = self._read_json(self.active_seed_path)
        return [SeedProduct.from_dict(item) for item in payload.get("seeds", [])]

    def save_active_seeds(self, seeds: list[SeedProduct]) -> None:
        payload = self._read_json(self.active_seed_path) if self.active_seed_path.exists() else {}
        payload["seeds"] = [seed.to_dict() for seed in seeds]
        payload["updated_at"] = utc_now_iso()
        self._write_json(self.active_seed_path, payload)

    def load_used_seed_ids(self) -> set[str]:
        self.initialize_runtime()
        used: set[str] = set()
        for item in self._read_jsonl(self.used_seed_path):
            seed_id = item.get("seed_id")
            if seed_id:
                used.add(seed_id)
        return used

    def append_used_seeds(self, run_id: str, seeds: Iterable[SeedProduct], result: str) -> None:
        rows = [
            {
                "run_id": run_id,
                "seed_id": seed.seed_id,
                "title_or_keyword": seed.title_or_keyword,
                "result": result,
                "archived_at": utc_now_iso(),
            }
            for seed in seeds
        ]
        self._append_jsonl(self.used_seed_path, rows)

    def append_seed_blacklist(
        self,
        run_id: str,
        seed: SeedProduct,
        ozon_product_id: str,
        reason: str,
    ) -> None:
        existing = self._read_jsonl(self.seed_blacklist_path)
        if any(
            str(item.get("seed_id") or "") == seed.seed_id
            and str(item.get("ozon_product_id") or "") == ozon_product_id
            for item in existing
        ):
            return
        self._append_jsonl(
            self.seed_blacklist_path,
            [
                {
                    "run_id": run_id,
                    "seed_id": seed.seed_id,
                    "ozon_product_id": ozon_product_id,
                    "title_or_keyword": seed.title_or_keyword,
                    "reason_code": "supplier_not_found_by_user",
                    "reason": reason,
                    "blacklisted_at": utc_now_iso(),
                }
            ],
        )

    def load_blacklisted_seed_ids(self) -> set[str]:
        self.initialize_runtime()
        return {
            str(item.get("seed_id"))
            for item in self._read_jsonl(self.seed_blacklist_path)
            if item.get("seed_id")
        }

    def load_blacklisted_ozon_product_ids(self) -> set[str]:
        self.initialize_runtime()
        return {
            str(item.get("ozon_product_id"))
            for item in self._read_jsonl(self.seed_blacklist_path)
            if item.get("ozon_product_id")
        }

    def load_existing_products(self) -> list[ExistingStoreProduct]:
        self.initialize_runtime()
        return [ExistingStoreProduct.from_dict(item) for item in self._read_jsonl(self.existing_store_dedupe_path)]

    def replace_existing_products(self, products: list[ExistingStoreProduct]) -> None:
        self.initialize_runtime()
        self._write_text(self.existing_store_dedupe_path, "")
        self._append_jsonl(self.existing_store_dedupe_path, [product.to_dict() for product in products])
        self._write_json(
            self.existing_store_dedupe_meta_path,
            {
                "refreshed_at": utc_now_iso(),
                "product_count": len(products),
            },
        )

    def existing_store_dedupe_status(self) -> dict[str, Any]:
        self.initialize_runtime()
        product_count = len(self._read_jsonl(self.existing_store_dedupe_path))
        if not self.existing_store_dedupe_meta_path.exists():
            return {
                "ready": False,
                "product_count": product_count,
                "meta_path": str(self.existing_store_dedupe_meta_path),
                "refreshed_at": None,
            }
        meta = self._read_json(self.existing_store_dedupe_meta_path)
        return {
            "ready": True,
            "product_count": product_count,
            "meta_path": str(self.existing_store_dedupe_meta_path),
            "refreshed_at": meta.get("refreshed_at"),
        }

    def create_run_record(
        self,
        target_count: int,
        sampled_seeds: list[SeedProduct],
        random_seed: int,
        status: RunStatus,
    ) -> dict[str, Any]:
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        run_dir = self.run_dir(run_id)
        (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)
        run_record = {
            "run_id": run_id,
            "target_count": target_count,
            "status": status.value,
            "random_seed": random_seed,
            "seed_pool_version": self.context.config.seed_pool_version,
            "sampled_seed_ids": [seed.seed_id for seed in sampled_seeds],
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
        }
        self._write_json(run_dir / "run.json", run_record)
        self._write_json(run_dir / "sampled_seeds.json", [seed.to_dict() for seed in sampled_seeds])
        return run_record

    def create_workbench_batch_record(self, target_count: int) -> dict[str, Any]:
        run_id = f"wb-{uuid.uuid4().hex[:12]}"
        run_dir = self.run_dir(run_id)
        (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)
        run_record = {
            "kind": "workbench_batch",
            "run_id": run_id,
            "target_count": target_count,
            "status": WorkbenchState.CREATED.value,
            "publish_locked": True,
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
        }
        self._write_json(run_dir / "run.json", run_record)
        return run_record

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def load_run(self, run_id: str) -> dict[str, Any]:
        return self._read_json(self.run_dir(run_id) / "run.json")

    def list_runs(self) -> list[dict[str, Any]]:
        self.initialize_runtime()
        runs: list[dict[str, Any]] = []
        for directory in self.runs_dir.iterdir():
            run_path = directory / "run.json"
            if not directory.is_dir() or not run_path.exists():
                continue
            try:
                runs.append(self._read_json(run_path))
            except (json.JSONDecodeError, OSError, TypeError):
                continue
        return sorted(
            runs,
            key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
            reverse=True,
        )

    def workbench_run_ids(self) -> list[str]:
        return [
            str(run["run_id"])
            for run in self.list_runs()
            if run.get("kind") == "workbench_batch" and str(run.get("run_id") or "").startswith("wb-")
        ]

    def clear_workbench_batches(self) -> dict[str, Any]:
        self.initialize_runtime()
        runs_root = self.runs_dir.resolve(strict=False)
        deleted_run_ids: list[str] = []
        skipped_entries: list[str] = []
        with _JSON_WRITE_LOCK:
            for candidate in list(self.runs_dir.iterdir()):
                if not candidate.is_dir() or not candidate.name.startswith("wb-"):
                    continue
                resolved = candidate.resolve(strict=False)
                if resolved.parent != runs_root:
                    skipped_entries.append(candidate.name)
                    continue
                run_path = candidate / "run.json"
                try:
                    run = self._read_json(run_path)
                except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
                    skipped_entries.append(candidate.name)
                    continue
                if run.get("kind") != "workbench_batch" or run.get("run_id") != candidate.name:
                    skipped_entries.append(candidate.name)
                    continue
                shutil.rmtree(candidate)
                deleted_run_ids.append(candidate.name)
        return {
            "deleted_run_ids": sorted(deleted_run_ids),
            "deleted_count": len(deleted_run_ids),
            "skipped_entries": sorted(skipped_entries),
        }

    def save_run(self, run_record: dict[str, Any]) -> None:
        run_record["updated_at"] = utc_now_iso()
        self._write_json(self.run_dir(run_record["run_id"]) / "run.json", run_record)

    def append_run_event(
        self,
        run_id: str,
        event_type: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> RunEvent:
        event = RunEvent(run_id=run_id, event_type=event_type, message=message, data=data or {})
        self._append_jsonl(self.run_dir(run_id) / "events.jsonl", [event.to_dict()])
        return event

    def load_run_events(self, run_id: str) -> list[RunEvent]:
        return [RunEvent.from_dict(item) for item in self._read_jsonl(self.run_dir(run_id) / "events.jsonl")]

    def load_sampled_seeds(self, run_id: str) -> list[SeedProduct]:
        return [SeedProduct.from_dict(item) for item in self._read_json(self.run_dir(run_id) / "sampled_seeds.json")]

    def save_sampled_seeds(self, run_id: str, seeds: list[SeedProduct]) -> None:
        self._write_json(self.run_dir(run_id) / "sampled_seeds.json", [seed.to_dict() for seed in seeds])

    def save_attribute_template_result(self, run_id: str, payload: dict[str, Any]) -> Path:
        path = self.run_dir(run_id) / "attribute_template_result.json"
        self._write_json(path, payload)
        return path

    def load_attribute_template_result(self, run_id: str) -> dict[str, Any]:
        return self._read_json(self.run_dir(run_id) / "attribute_template_result.json")

    def save_attribute_template_contract(self, run_id: str, payload: dict[str, Any]) -> Path:
        path = self.run_dir(run_id) / "attribute_template_contract.json"
        self._write_json(path, payload)
        return path

    def load_attribute_template_contract(self, run_id: str) -> dict[str, Any]:
        return self._read_json(self.run_dir(run_id) / "attribute_template_contract.json")

    def save_ozon_collection_contract(self, run_id: str, payload: dict[str, Any]) -> Path:
        path = self.run_dir(run_id) / "ozon_collection_contract.json"
        self._write_json(path, payload)
        return path

    def load_ozon_collection_contract(self, run_id: str) -> dict[str, Any]:
        return self._read_json(self.run_dir(run_id) / "ozon_collection_contract.json")

    def save_ozon_collection_draft(self, run_id: str, payload: dict[str, Any]) -> Path:
        path = self.run_dir(run_id) / "ozon_collection_draft.json"
        self._write_json(path, payload)
        return path

    def load_ozon_collection_draft(self, run_id: str) -> dict[str, Any]:
        return self._read_json(self.run_dir(run_id) / "ozon_collection_draft.json")

    def save_ozon_collection_result(self, run_id: str, payload: dict[str, Any]) -> Path:
        path = self.run_dir(run_id) / "ozon_collection_result.json"
        self._write_json(path, payload)
        return path

    def load_ozon_collection_result(self, run_id: str) -> dict[str, Any]:
        return self._read_json(self.run_dir(run_id) / "ozon_collection_result.json")

    def save_supplier_review(self, run_id: str, payload: dict[str, Any]) -> Path:
        path = self.run_dir(run_id) / "supplier_review.json"
        self._write_json(path, payload)
        return path

    def load_supplier_review(self, run_id: str) -> dict[str, Any]:
        return self._read_json(self.run_dir(run_id) / "supplier_review.json")

    def save_supplier_selection_draft(self, run_id: str, payload: dict[str, Any]) -> Path:
        path = self.run_dir(run_id) / "supplier_selection_draft.json"
        self._write_json(path, payload)
        return path

    def load_supplier_selection_draft(self, run_id: str) -> dict[str, Any]:
        return self._read_json(self.run_dir(run_id) / "supplier_selection_draft.json")

    def save_supplier_collection_contract(self, run_id: str, payload: dict[str, Any]) -> Path:
        path = self.run_dir(run_id) / "supplier_collection_contract.json"
        self._write_json(path, payload)
        return path

    def load_supplier_collection_contract(self, run_id: str) -> dict[str, Any]:
        return self._read_json(self.run_dir(run_id) / "supplier_collection_contract.json")

    def save_supplier_collection_progress(self, run_id: str, payload: dict[str, Any]) -> Path:
        path = self.run_dir(run_id) / "supplier_collection_progress.json"
        self._write_json(path, payload)
        return path

    def load_supplier_collection_progress(self, run_id: str) -> dict[str, Any]:
        return self._read_json(self.run_dir(run_id) / "supplier_collection_progress.json")

    def save_supplier_collection_result(self, run_id: str, payload: dict[str, Any]) -> Path:
        path = self.run_dir(run_id) / "supplier_collection_result.json"
        self._write_json(path, payload)
        return path

    def load_supplier_collection_result(self, run_id: str) -> dict[str, Any]:
        return self._read_json(self.run_dir(run_id) / "supplier_collection_result.json")

    def save_supplier_sku_selections(self, run_id: str, payload: dict[str, Any]) -> Path:
        path = self.run_dir(run_id) / "supplier_sku_selections.json"
        self._write_json(path, payload)
        return path

    def load_supplier_sku_selections(self, run_id: str) -> dict[str, Any]:
        return self._read_json(self.run_dir(run_id) / "supplier_sku_selections.json")

    def save_pricing_settings(self, payload: dict[str, Any]) -> Path:
        self._write_json(self.pricing_settings_path, payload)
        return self.pricing_settings_path

    def load_pricing_settings(self) -> dict[str, Any]:
        if not self.pricing_settings_path.exists():
            return PricingPolicy.default().to_dict()
        return self._read_json(self.pricing_settings_path)

    def save_public_media_settings(self, payload: dict[str, Any]) -> Path:
        self._write_json(self.public_media_settings_path, payload)
        return self.public_media_settings_path

    def load_public_media_settings(self) -> dict[str, Any]:
        if not self.public_media_settings_path.exists():
            return {"base_url": ""}
        return self._read_json(self.public_media_settings_path)

    def save_pricing_evidence(self, run_id: str, payload: dict[str, Any]) -> Path:
        path = self.run_dir(run_id) / "pricing_evidence.json"
        self._write_json(path, payload)
        return path

    def load_pricing_evidence(self, run_id: str) -> dict[str, Any]:
        return self._read_json(self.run_dir(run_id) / "pricing_evidence.json")

    def save_subject_masters(self, run_id: str, payload: dict[str, Any]) -> Path:
        path = self.run_dir(run_id) / "subject_masters.json"
        self._write_json(path, payload)
        return path

    def load_subject_masters(self, run_id: str) -> dict[str, Any]:
        return self._read_json(self.run_dir(run_id) / "subject_masters.json")

    def save_generated_content_result(self, run_id: str, payload: dict[str, Any]) -> Path:
        path = self.run_dir(run_id) / "generated_content_result.json"
        self._write_json(path, payload)
        return path

    def load_generated_content_result(self, run_id: str) -> dict[str, Any]:
        return self._read_json(self.run_dir(run_id) / "generated_content_result.json")

    def save_required_attribute_evidence(
        self,
        run_id: str,
        payload: dict[str, Any],
    ) -> Path:
        path = self.run_dir(run_id) / "required_attribute_evidence.json"
        self._write_json(path, payload)
        return path

    def load_required_attribute_evidence(self, run_id: str) -> dict[str, Any]:
        return self._read_json(
            self.run_dir(run_id) / "required_attribute_evidence.json"
        )

    def save_upload_draft(self, run_id: str, payload: dict[str, Any]) -> Path:
        path = self.run_dir(run_id) / "upload_draft.json"
        self._write_json(path, payload)
        return path

    def load_upload_draft(self, run_id: str) -> dict[str, Any]:
        return self._read_json(self.run_dir(run_id) / "upload_draft.json")

    def save_upload_previews(self, run_id: str, payload: dict[str, Any]) -> Path:
        path = self.run_dir(run_id) / "upload_previews.json"
        self._write_json(path, payload)
        return path

    def load_upload_previews(self, run_id: str) -> dict[str, Any]:
        return self._read_json(self.run_dir(run_id) / "upload_previews.json")

    def save_upload_submissions(self, run_id: str, payload: dict[str, Any]) -> Path:
        path = self.run_dir(run_id) / "upload_submissions.json"
        self._write_json(path, payload)
        return path

    def load_upload_submissions(self, run_id: str) -> dict[str, Any]:
        return self._read_json(self.run_dir(run_id) / "upload_submissions.json")

    def image_task_pending_dir(self) -> Path:
        path = self.runtime_root / "image_tasks" / "pending"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_image_task_package(
        self,
        package_id: str,
        payload: dict[str, Any],
    ) -> Path:
        safe_package_id = re.sub(
            r"[^A-Za-z0-9_.-]+",
            "-",
            str(package_id or "").strip(),
        ).strip(".-")
        if not safe_package_id:
            raise ValueError("Image task package_id must contain a safe filename.")
        path = self.image_task_pending_dir() / f"{safe_package_id}.json"
        self._write_json(path, payload)
        return path

    def save_browser_bridge_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._initialize_runtime_once()
        now = utc_now_iso()
        if payload.get("connection_only"):
            status = self.load_browser_bridge_status()
            status["connection_updated_at"] = now
            status["connection_source"] = payload.get("source")
            if payload.get("extension_version"):
                status["extension_version"] = payload.get("extension_version")
        else:
            status = dict(payload)
            status.pop("connection_only", None)
            status["updated_at"] = now
            status["connection_updated_at"] = now
            status["connection_source"] = payload.get("source")
        self._write_json(self.browser_bridge_status_path, status)
        return status

    def load_browser_bridge_status(self) -> dict[str, Any]:
        self._initialize_runtime_once()
        if not self.browser_bridge_status_path.exists():
            return {
                "online": False,
                "updated_at": None,
                "source": None,
                "run_id": None,
                "task_type": None,
                "stage": None,
                "code": "browser_bridge.no_heartbeat",
                "message": "No browser bridge heartbeat has been received.",
            }
        return self._read_json(self.browser_bridge_status_path)

    def clear_browser_bridge_task_status(self) -> dict[str, Any]:
        status = self.load_browser_bridge_status()
        status.update(
            {
                "source": status.get("connection_source") or status.get("source"),
                "run_id": None,
                "task_type": None,
                "stage": "idle",
                "code": "browser_task.none",
                "message": "All workbench batch task references were cleared.",
                "url": "",
                "details": None,
            }
        )
        status["updated_at"] = status.get("connection_updated_at") or status.get("updated_at")
        self._write_json(self.browser_bridge_status_path, status)
        return status

    def append_rejected_seed_attempt(
        self,
        run_id: str,
        seed: SeedProduct,
        reason: str,
        replacement_seed_id: str | None = None,
    ) -> None:
        self._append_jsonl(
            self.run_dir(run_id) / "rejected_seed_attempts.jsonl",
            [
                {
                    "run_id": run_id,
                    "seed": seed.to_dict(),
                    "seed_id": seed.seed_id,
                    "title_or_keyword": seed.title_or_keyword,
                    "reason": reason,
                    "replacement_seed_id": replacement_seed_id,
                    "rejected_at": utc_now_iso(),
                }
            ],
        )

    def load_rejected_seed_attempts(self, run_id: str) -> list[dict[str, Any]]:
        return self._read_jsonl(self.run_dir(run_id) / "rejected_seed_attempts.jsonl")

    def append_supplier_rejection(self, run_id: str, payload: dict[str, Any]) -> None:
        self._append_jsonl(
            self.run_dir(run_id) / "supplier_rejections.jsonl",
            [{**payload, "run_id": run_id, "rejected_at": utc_now_iso()}],
        )

    def load_supplier_rejections(self, run_id: str) -> list[dict[str, Any]]:
        return self._read_jsonl(self.run_dir(run_id) / "supplier_rejections.jsonl")

    def append_collection_pairs(self, run_id: str, pairs: list[CollectionPair]) -> None:
        self._append_jsonl(self.run_dir(run_id) / "collection_pairs.jsonl", [pair.to_dict() for pair in pairs])

    def load_collection_pairs(self, run_id: str) -> list[CollectionPair]:
        path = self.run_dir(run_id) / "collection_pairs.jsonl"
        return [CollectionPair.from_dict(item) for item in self._read_jsonl(path)]

    def write_evidence_csv(self, run_id: str, pairs: list[CollectionPair]) -> Path:
        path = self.run_dir(run_id) / "evidence.csv"
        fieldnames = [
            "pair_id",
            "run_id",
            "seed_pool_version",
            "seed_id",
            "seed_title_or_keyword",
            "seed_source_language",
            "ozon_query_terms_ru",
            "query_generation_method",
            "final_decision",
            "ozon_url",
            "ozon_title",
            "ozon_seller_name",
            "domestic_seller_confidence",
            "category_path",
            "leaf_category",
            "category_url",
            "category_id",
            "ozon_target_sku_options",
            "ozon_content_score_evidence",
            "supplier_url",
            "supplier_title",
            "supplier_shop_name",
            "exact_match_confidence",
            "matched_supplier_sku_options",
            "supplier_domestic_shipping_fee",
            "supplier_domestic_shipping_destination",
            "supplier_domestic_shipping_evidence",
            "single_sku_truth_notes",
            "captured_at",
        ]
        run = self.load_run(run_id)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for pair in pairs:
                writer.writerow(
                    {
                        "pair_id": pair.pair_id,
                        "run_id": run_id,
                        "seed_pool_version": run.get("seed_pool_version", ""),
                        "seed_id": pair.seed_product.seed_id,
                        "seed_title_or_keyword": pair.seed_product.title_or_keyword,
                        "seed_source_language": pair.seed_product.source_language,
                        "ozon_query_terms_ru": "; ".join(pair.seed_product.ozon_query_terms_ru),
                        "query_generation_method": pair.seed_product.query_generation_method or "",
                        "final_decision": pair.final_decision.value,
                        "ozon_url": pair.ozon_candidate.ozon_url,
                        "ozon_title": pair.ozon_candidate.title,
                        "ozon_seller_name": pair.ozon_candidate.seller_name,
                        "domestic_seller_confidence": pair.ozon_candidate.domestic_seller_decision.get("confidence", ""),
                        "category_path": pair.ozon_candidate.category_path or "",
                        "leaf_category": pair.ozon_candidate.leaf_category or "",
                        "category_url": pair.ozon_candidate.category_url or "",
                        "category_id": pair.ozon_candidate.category_id or "",
                        "ozon_target_sku_options": json.dumps(pair.ozon_candidate.target_sku.selected_options, ensure_ascii=False),
                        "ozon_content_score_evidence": json.dumps(pair.ozon_candidate.content_score_evidence, ensure_ascii=False),
                        "supplier_url": pair.supplier_match.supplier_url,
                        "supplier_title": pair.supplier_match.title,
                        "supplier_shop_name": pair.supplier_match.shop_name,
                        "exact_match_confidence": pair.supplier_match.exact_match_decision.get("confidence", ""),
                        "matched_supplier_sku_options": json.dumps(pair.supplier_match.matched_supplier_sku.selected_options, ensure_ascii=False),
                        "supplier_domestic_shipping_fee": pair.supplier_match.domestic_shipping_fee or "",
                        "supplier_domestic_shipping_destination": pair.supplier_match.domestic_shipping_destination or "",
                        "supplier_domestic_shipping_evidence": json.dumps(pair.supplier_match.domestic_shipping_evidence, ensure_ascii=False),
                        "single_sku_truth_notes": "; ".join(pair.reasons),
                        "captured_at": pair.created_at,
                    }
                )
        return path

    def remove_active_seeds(self, seed_ids: set[str]) -> list[SeedProduct]:
        seeds = self.load_active_seeds()
        removed = [seed for seed in seeds if seed.seed_id in seed_ids]
        remaining = [seed for seed in seeds if seed.seed_id not in seed_ids]
        self.save_active_seeds(remaining)
        return removed

    def sample_seeds(self, seeds: list[SeedProduct], target_count: int, random_seed: int) -> list[SeedProduct]:
        sampler = random.Random(random_seed)
        return sampler.sample(seeds, target_count)

    def _read_json(self, path: Path) -> Any:
        with _JSON_WRITE_LOCK:
            return json.loads(path.read_text(encoding="utf-8"))

    def _initialize_runtime_once(self) -> None:
        if self.active_seed_path.exists():
            return
        with _JSON_WRITE_LOCK:
            if not self.active_seed_path.exists():
                self.initialize_runtime()

    def _write_json(self, path: Path, payload: Any) -> None:
        with _JSON_WRITE_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                temporary.write_text(
                    json.dumps(to_plain(payload), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                for attempt in range(6):
                    try:
                        temporary.replace(path)
                        break
                    except PermissionError:
                        if attempt == 5:
                            raise
                        time.sleep(0.02 * (attempt + 1))
            finally:
                temporary.unlink(missing_ok=True)

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    def _append_jsonl(self, path: Path, rows: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(to_plain(row), ensure_ascii=False) + "\n")

    def _write_text(self, path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
