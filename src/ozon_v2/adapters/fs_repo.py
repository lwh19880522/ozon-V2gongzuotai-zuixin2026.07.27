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
from ozon_v2.adapters.secret_storage import (
    is_secret_envelope,
    protect_mapping,
    unprotect_mapping,
)
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
    def product_identity_ledger_path(self) -> Path:
        return self.state_dir / "used_product_identities.jsonl"

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
    def credential_assistant_state_path(self) -> Path:
        return self.state_dir / "credential_assistant_state.json"

    @property
    def browser_bridge_status_path(self) -> Path:
        return self.state_dir / "browser_bridge.status.json"

    def initialize_runtime(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        for ledger_path in (
            self.used_seed_path,
            self.seed_blacklist_path,
            self.existing_store_dedupe_path,
            self.product_identity_ledger_path,
        ):
            if not ledger_path.exists():
                ledger_path.touch()

        bundled_package = self._read_json(self.context.paths.initial_seed_json)
        bundled_version = str(bundled_package["package_version"])
        installed_version = ""
        if self.config_initial_seed_path.exists():
            try:
                installed_version = str(
                    self._read_json(self.config_initial_seed_path).get("package_version") or ""
                )
            except (json.JSONDecodeError, OSError, TypeError):
                installed_version = ""
        if installed_version != bundled_version:
            shutil.copy2(self.context.paths.initial_seed_json, self.config_initial_seed_path)

        if not self.active_seed_path.exists():
            self._write_json(
                self.active_seed_path,
                {
                    "package_version": bundled_version,
                    "initialized_at": utc_now_iso(),
                    "seeds": bundled_package["seeds"],
                },
            )
        else:
            try:
                active_package = self._read_json(self.active_seed_path)
            except (json.JSONDecodeError, OSError, TypeError):
                active_package = {}
            active_version = str(active_package.get("package_version") or "")
            if active_version != bundled_version:
                used_seed_ids = {
                    str(item["seed_id"])
                    for item in self._read_jsonl(self.used_seed_path)
                    if item.get("seed_id")
                }
                blacklisted_seed_ids = {
                    str(item["seed_id"])
                    for item in self._read_jsonl(self.seed_blacklist_path)
                    if item.get("seed_id")
                }
                excluded_seed_ids = used_seed_ids | blacklisted_seed_ids
                migrated_seeds = [
                    seed
                    for seed in bundled_package["seeds"]
                    if str(seed.get("seed_id") or "") not in excluded_seed_ids
                ]
                self._write_json(
                    self.active_seed_path,
                    {
                        "package_version": bundled_version,
                        "initialized_at": utc_now_iso(),
                        "upgraded_from_package_version": active_version or None,
                        "excluded_used_seed_count": len(used_seed_ids),
                        "excluded_blacklisted_seed_count": len(blacklisted_seed_ids),
                        "seeds": migrated_seeds,
                    },
                )
        self._reconcile_active_seed_exclusions()
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
        self._write_json(
            self.credentials_path,
            protect_mapping(credentials.to_private_dict()),
        )
        if self.credentials_path.exists():
            try:
                self.credentials_path.chmod(0o600)
            except OSError:
                pass

    def load_credentials(self) -> SellerCredentials | None:
        if not self.credentials_path.exists():
            return None
        stored = self._read_json(self.credentials_path)
        if is_secret_envelope(stored):
            return SellerCredentials.from_dict(unprotect_mapping(stored))
        credentials = SellerCredentials.from_dict(stored)
        self._write_json(
            self.credentials_path,
            protect_mapping(credentials.to_private_dict()),
        )
        return credentials

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
        payload.setdefault("package_version", self.context.config.seed_pool_version)
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

    def load_used_seed_identity_keys(self) -> set[str]:
        self.initialize_runtime()
        return {
            self._seed_identity_key_from_row(item)
            for item in self._read_jsonl(self.used_seed_path)
            if item.get("seed_id") or item.get("seed_identity_key")
        }

    def append_used_seeds(self, run_id: str, seeds: Iterable[SeedProduct], result: str) -> None:
        seed_list = list(seeds)
        if not seed_list:
            return
        with _JSON_WRITE_LOCK:
            existing = self._read_jsonl(self.used_seed_path)
            existing_keys = {
                self._seed_identity_key_from_row(item)
                for item in existing
                if item.get("seed_id") or item.get("seed_identity_key")
            }
            rows: list[dict[str, Any]] = []
            for seed in seed_list:
                identity_key = self.seed_identity_key(seed)
                if identity_key in existing_keys:
                    continue
                rows.append(
                    {
                        "run_id": run_id,
                        "seed_id": seed.seed_id,
                        "seed_identity_key": identity_key,
                        "title_or_keyword": seed.title_or_keyword,
                        "product_clue": seed.product_clue,
                        "result": result,
                        "archived_at": utc_now_iso(),
                    }
                )
                existing_keys.add(identity_key)
            self._append_jsonl(self.used_seed_path, rows)
        self.remove_active_seed_identities({self.seed_identity_key(seed) for seed in seed_list})

    def append_seed_blacklist(
        self,
        run_id: str,
        seed: SeedProduct,
        ozon_product_id: str,
        reason: str,
        *,
        reason_code: str = "supplier_not_found_by_user",
    ) -> None:
        identity_key = self.seed_identity_key(seed)
        with _JSON_WRITE_LOCK:
            existing = self._read_jsonl(self.seed_blacklist_path)
            if not any(
                self._seed_identity_key_from_row(item) == identity_key
                and str(item.get("ozon_product_id") or "") == ozon_product_id
                for item in existing
            ):
                self._append_jsonl(
                    self.seed_blacklist_path,
                    [
                        {
                            "run_id": run_id,
                            "seed_id": seed.seed_id,
                            "seed_identity_key": identity_key,
                            "ozon_product_id": ozon_product_id,
                            "title_or_keyword": seed.title_or_keyword,
                            "product_clue": seed.product_clue,
                            "reason_code": reason_code,
                            "reason": reason,
                            "blacklisted_at": utc_now_iso(),
                        }
                    ],
                )
        self.remove_active_seed_identities({identity_key})

    def append_ozon_product_blacklist(
        self,
        run_id: str,
        ozon_product_id: str,
        reason: str,
        *,
        source_seed_id: str = "",
        reason_code: str = "category_subject_mismatch",
    ) -> None:
        normalized_product_id = str(ozon_product_id or "").strip()
        if not normalized_product_id:
            raise ValueError("ozon_product_id is required")
        with _JSON_WRITE_LOCK:
            existing = self._read_jsonl(self.seed_blacklist_path)
            if any(
                str(item.get("ozon_product_id") or "").strip()
                == normalized_product_id
                for item in existing
            ):
                return
            self._append_jsonl(
                self.seed_blacklist_path,
                [
                    {
                        "run_id": run_id,
                        "source_seed_id": str(source_seed_id or "").strip(),
                        "ozon_product_id": normalized_product_id,
                        "reason_code": reason_code,
                        "reason": str(reason or "").strip(),
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

    def load_blacklisted_seed_identity_keys(self) -> set[str]:
        self.initialize_runtime()
        return {
            self._seed_identity_key_from_row(item)
            for item in self._read_jsonl(self.seed_blacklist_path)
            if item.get("seed_id") or item.get("seed_identity_key")
        }

    def load_blacklisted_ozon_product_ids(self) -> set[str]:
        self.initialize_runtime()
        return {
            str(item.get("ozon_product_id"))
            for item in self._read_jsonl(self.seed_blacklist_path)
            if item.get("ozon_product_id")
        }

    @staticmethod
    def _normalize_product_identity(identity_type: str, value: Any) -> str:
        normalized = str(value or "").strip().casefold()
        if identity_type not in {"ozon_product_id", "supplier_offer_id"}:
            raise ValueError(f"Unsupported product identity type: {identity_type}")
        return normalized

    @staticmethod
    def _supplier_offer_id_from_product(product: dict[str, Any]) -> str:
        offer_id = str(
            product.get("offer_id") or product.get("supplier_product_id") or ""
        ).strip()
        if offer_id:
            return offer_id
        supplier_url = str(
            product.get("final_url") or product.get("supplier_url") or ""
        )
        match = re.search(r"/offer/(\d+)\.html", supplier_url)
        return match.group(1) if match else ""

    def _historical_product_identity_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in self._read_jsonl(self.seed_blacklist_path):
            product_id = self._normalize_product_identity(
                "ozon_product_id", item.get("ozon_product_id")
            )
            if product_id:
                rows.append(
                    {
                        "identity_type": "ozon_product_id",
                        "identity_value": product_id,
                        "run_id": str(item.get("run_id") or "seed_blacklist"),
                        "seed_id": str(item.get("seed_id") or ""),
                        "source": "seed_blacklist",
                        "archived_at": str(item.get("blacklisted_at") or utc_now_iso()),
                    }
                )
        if not self.runs_dir.exists():
            return rows
        for run_dir in sorted(self.runs_dir.iterdir(), key=lambda path: path.name):
            if not run_dir.is_dir():
                continue
            run_id = run_dir.name
            ozon_path = run_dir / "ozon_collection_result.json"
            if ozon_path.is_file():
                try:
                    ozon_payload = self._read_json(ozon_path)
                except (OSError, json.JSONDecodeError, TypeError, ValueError):
                    ozon_payload = {}
                for item in ozon_payload.get("ozon_candidates", []):
                    if not isinstance(item, dict):
                        continue
                    product_id = self._normalize_product_identity(
                        "ozon_product_id", item.get("ozon_product_id")
                    )
                    if product_id:
                        rows.append(
                            {
                                "identity_type": "ozon_product_id",
                                "identity_value": product_id,
                                "run_id": run_id,
                                "seed_id": str(item.get("seed_id") or ""),
                                "source": "historical_ozon_collection",
                                "archived_at": utc_now_iso(),
                            }
                        )
            for filename in (
                "supplier_selection_draft.json",
                "supplier_collection_result.json",
            ):
                supplier_path = run_dir / filename
                if not supplier_path.is_file():
                    continue
                try:
                    supplier_payload = self._read_json(supplier_path)
                except (OSError, json.JSONDecodeError, TypeError, ValueError):
                    continue
                for item in supplier_payload.get("supplier_products", []):
                    if not isinstance(item, dict):
                        continue
                    offer_id = self._normalize_product_identity(
                        "supplier_offer_id",
                        self._supplier_offer_id_from_product(item),
                    )
                    if offer_id:
                        rows.append(
                            {
                                "identity_type": "supplier_offer_id",
                                "identity_value": offer_id,
                                "run_id": run_id,
                                "seed_id": str(item.get("seed_id") or ""),
                                "source": f"historical_{filename[:-5]}",
                                "archived_at": utc_now_iso(),
                            }
                        )
        return rows

    def _reconcile_product_identity_ledger_locked(self) -> list[dict[str, Any]]:
        existing = self._read_jsonl(self.product_identity_ledger_path)
        existing_keys = {
            (
                str(item.get("identity_type") or ""),
                str(item.get("identity_value") or "").casefold(),
            )
            for item in existing
            if item.get("identity_type") and item.get("identity_value")
        }
        additions: list[dict[str, Any]] = []
        for item in self._historical_product_identity_rows():
            key = (item["identity_type"], item["identity_value"])
            if key in existing_keys:
                continue
            additions.append(item)
            existing_keys.add(key)
        if additions:
            self._append_jsonl(self.product_identity_ledger_path, additions)
        return existing + additions

    def load_used_product_identity_values(
        self,
        identity_type: str,
        *,
        exclude_run_id: str | None = None,
    ) -> set[str]:
        self.initialize_runtime()
        with _JSON_WRITE_LOCK:
            rows = self._reconcile_product_identity_ledger_locked()
        return {
            str(item.get("identity_value") or "")
            for item in rows
            if str(item.get("identity_type") or "") == identity_type
            and str(item.get("identity_value") or "")
            and (
                exclude_run_id is None
                or str(item.get("run_id") or "") != exclude_run_id
            )
        }

    def load_used_ozon_product_ids(self, *, exclude_run_id: str | None = None) -> set[str]:
        return self.load_used_product_identity_values(
            "ozon_product_id", exclude_run_id=exclude_run_id
        )

    def load_used_supplier_offer_ids(self, *, exclude_run_id: str | None = None) -> set[str]:
        return self.load_used_product_identity_values(
            "supplier_offer_id", exclude_run_id=exclude_run_id
        )

    def claim_product_identities(
        self,
        run_id: str,
        claims: Iterable[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        normalized_claims: list[dict[str, Any]] = []
        for raw_claim in claims:
            identity_type = str(raw_claim.get("identity_type") or "")
            identity_value = self._normalize_product_identity(
                identity_type, raw_claim.get("identity_value")
            )
            if not identity_value:
                continue
            normalized_claims.append(
                {
                    "identity_type": identity_type,
                    "identity_value": identity_value,
                    "run_id": run_id,
                    "seed_id": str(raw_claim.get("seed_id") or ""),
                    "source": str(raw_claim.get("source") or "workbench"),
                    "archived_at": utc_now_iso(),
                }
            )
        if not normalized_claims:
            return []
        self.initialize_runtime()
        with _JSON_WRITE_LOCK:
            existing = self._reconcile_product_identity_ledger_locked()
            by_identity = {
                (
                    str(item.get("identity_type") or ""),
                    str(item.get("identity_value") or "").casefold(),
                ): item
                for item in existing
                if item.get("identity_type") and item.get("identity_value")
            }
            conflicts: list[dict[str, Any]] = []
            pending: list[dict[str, Any]] = []
            for claim in normalized_claims:
                key = (claim["identity_type"], claim["identity_value"])
                previous = by_identity.get(key)
                if previous is not None:
                    same_owner = (
                        str(previous.get("run_id") or "") == run_id
                        and str(previous.get("seed_id") or "") == claim["seed_id"]
                    )
                    if not same_owner:
                        conflicts.append(
                            {
                                "identity_type": claim["identity_type"],
                                "identity_value": claim["identity_value"],
                                "requested_run_id": run_id,
                                "requested_seed_id": claim["seed_id"],
                                "previous_run_id": previous.get("run_id"),
                                "previous_seed_id": previous.get("seed_id"),
                                "previous_source": previous.get("source"),
                            }
                        )
                    continue
                pending.append(claim)
                by_identity[key] = claim
            if conflicts:
                return conflicts
            if pending:
                self._append_jsonl(self.product_identity_ledger_path, pending)
        return []

    def load_existing_products(self) -> list[ExistingStoreProduct]:
        self.initialize_runtime()
        return [ExistingStoreProduct.from_dict(item) for item in self._read_jsonl(self.existing_store_dedupe_path)]

    def load_local_uploaded_products(self) -> list[ExistingStoreProduct]:
        """Recover accepted uploads that may not be visible in Seller API yet."""
        self.initialize_runtime()
        products: list[ExistingStoreProduct] = []
        if not self.runs_dir.exists():
            return products
        for run_dir in sorted(self.runs_dir.iterdir(), key=lambda path: path.name):
            if not run_dir.is_dir():
                continue
            submissions_path = run_dir / "upload_submissions.json"
            previews_path = run_dir / "upload_previews.json"
            if not submissions_path.is_file() or not previews_path.is_file():
                continue
            try:
                submissions = self._read_json(submissions_path)
                previews = self._read_json(previews_path)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                continue
            submission_items = submissions.get("items") if isinstance(submissions, dict) else None
            preview_items = previews.get("items") if isinstance(previews, dict) else None
            if not isinstance(submission_items, dict) or not isinstance(preview_items, dict):
                continue
            try:
                seeds_by_id = {
                    seed.seed_id: seed for seed in self.load_sampled_seeds(run_dir.name)
                }
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                seeds_by_id = {}
            try:
                ozon_result = self.load_ozon_collection_result(run_dir.name)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                ozon_result = {}
            ozon_by_seed = {
                str(item.get("seed_id") or ""): item
                for item in ozon_result.get("ozon_candidates", [])
                if isinstance(item, dict) and str(item.get("seed_id") or "")
            }
            try:
                supplier_result = self.load_supplier_collection_result(run_dir.name)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                supplier_result = {}
            supplier_by_seed = {
                str(item.get("seed_id") or ""): item
                for item in supplier_result.get("supplier_products", [])
                if isinstance(item, dict) and str(item.get("seed_id") or "")
            }
            for seed_id, submission in submission_items.items():
                if not isinstance(submission, dict):
                    continue
                status = str(submission.get("status") or "").strip().casefold()
                if status not in {"accepted_by_ozon", "submitted", "imported"}:
                    continue
                preview = preview_items.get(seed_id)
                seller_item = preview.get("seller_api_item") if isinstance(preview, dict) else None
                seller_item = seller_item if isinstance(seller_item, dict) else {}
                offer_id = str(
                    submission.get("offer_id") or seller_item.get("offer_id") or ""
                ).strip()
                if not offer_id:
                    continue
                product_id = ""
                for status_key in ("seller_api_status", "seller_api_import_status"):
                    status_payload = submission.get(status_key)
                    status_items = status_payload.get("items") if isinstance(status_payload, dict) else None
                    if not isinstance(status_items, list):
                        continue
                    matching = next(
                        (
                            item
                            for item in status_items
                            if isinstance(item, dict)
                            and str(item.get("offer_id") or "").strip() == offer_id
                            and item.get("product_id") not in (None, "")
                        ),
                        None,
                    )
                    if matching is not None:
                        product_id = str(matching["product_id"])
                        break
                title = str(seller_item.get("name") or offer_id).strip()
                seed = seeds_by_id.get(str(seed_id))
                ozon_candidate = ozon_by_seed.get(str(seed_id)) or {}
                supplier_product = supplier_by_seed.get(str(seed_id)) or {}
                supplier_offer_id = str(
                    supplier_product.get("offer_id")
                    or supplier_product.get("supplier_product_id")
                    or ""
                ).strip()
                products.append(
                    ExistingStoreProduct(
                        store_product_id=product_id or f"offer:{offer_id}",
                        offer_id_when_available=offer_id,
                        title=title,
                        normalized_identity_key=re.sub(r"[\W_]+", "", title.casefold()),
                        source_seed_identity_key=(
                            self.seed_identity_key(seed) if seed is not None else None
                        ),
                        seed_subject_identity_key=(
                            re.sub(
                                r"[\W_]+",
                                "",
                                str(
                                    seed.title_or_keyword or seed.product_clue
                                ).casefold(),
                            )
                            if seed is not None
                            else None
                        ),
                        source_ozon_product_id=str(
                            ozon_candidate.get("ozon_product_id") or ""
                        ).strip()
                        or None,
                        source_ozon_title=str(
                            ozon_candidate.get("title") or ""
                        ).strip()
                        or None,
                        supplier_offer_id=supplier_offer_id or None,
                        supplier_title=str(
                            supplier_product.get("title") or ""
                        ).strip()
                        or None,
                        source_captured_at=str(
                            submission.get("submitted_at") or utc_now_iso()
                        ),
                        notes=f"recovered from local accepted upload {run_dir.name}:{seed_id}",
                    )
                )
        return products

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

    def merge_existing_products(self, products: list[ExistingStoreProduct]) -> dict[str, int]:
        """Merge the latest Seller API view into the durable store denylist.

        Products absent from a later API response remain protected. Ozon may hide
        archived or temporarily unavailable cards from a later listing response;
        disappearance from that snapshot is not permission to publish a duplicate.
        """
        self.initialize_runtime()
        existing = self.load_existing_products()
        merged: list[ExistingStoreProduct] = []
        index_by_key: dict[str, int] = {}

        def identities(product: ExistingStoreProduct) -> list[str]:
            keys: list[str] = []
            product_id = str(product.store_product_id or "").strip()
            if product_id:
                keys.append(f"product:{product_id}")
            offer_id = str(product.offer_id_when_available or "").strip().casefold()
            if offer_id:
                keys.append(f"offer:{offer_id}")
            source_ozon_product_id = str(
                product.source_ozon_product_id or ""
            ).strip().casefold()
            if source_ozon_product_id:
                keys.append(f"source_ozon:{source_ozon_product_id}")
            supplier_offer_id = str(product.supplier_offer_id or "").strip().casefold()
            if supplier_offer_id:
                keys.append(f"supplier_offer:{supplier_offer_id}")
            source_seed_identity_key = str(
                product.source_seed_identity_key or ""
            ).strip().casefold()
            if source_seed_identity_key:
                keys.append(f"source_seed:{source_seed_identity_key}")
            if not keys:
                keys.append(f"title:{product.normalized_identity_key}")
            return keys

        def preserve_lineage(
            previous: ExistingStoreProduct,
            current: ExistingStoreProduct,
        ) -> ExistingStoreProduct:
            combined = previous.to_dict()
            for key, value in current.to_dict().items():
                if value not in (None, "", [], {}):
                    combined[key] = value
            return ExistingStoreProduct.from_dict(combined)

        for product in existing + list(products):
            keys = identities(product)
            matched_index = next(
                (index_by_key[key] for key in keys if key in index_by_key),
                None,
            )
            if matched_index is None:
                matched_index = len(merged)
                merged.append(product)
            else:
                merged[matched_index] = preserve_lineage(
                    merged[matched_index],
                    product,
                )
            for key in identities(merged[matched_index]):
                index_by_key[key] = matched_index
        self.replace_existing_products(merged)
        return {
            "fetched_product_count": len(products),
            "existing_product_count": len(merged),
            "preserved_history_count": max(0, len(merged) - len(products)),
        }

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
        normalized = str(run_id or "").strip()
        if not self._is_recognized_batch_directory_name(normalized):
            raise ValueError("run_id must be a safe workbench or legacy batch identifier")
        runs_root = self.runs_dir.resolve(strict=False)
        candidate = (runs_root / normalized).resolve(strict=False)
        if candidate.parent != runs_root:
            raise ValueError("run_id resolves outside the workbench runs directory")
        return candidate

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
        self.initialize_runtime()
        return sorted(
            candidate.name
            for candidate in self.runs_dir.iterdir()
            if candidate.is_dir() and self._is_recognized_batch_directory_name(candidate.name)
        )

    @staticmethod
    def is_current_workbench_run(run: dict[str, Any], directory_name: str | None = None) -> bool:
        run_id = str(run.get("run_id") or "").strip()
        if run.get("kind") != "workbench_batch" or not run_id.startswith("wb-"):
            return False
        if directory_name is not None and run_id != directory_name:
            return False
        try:
            WorkbenchState(str(run.get("status") or ""))
        except ValueError:
            return False
        return True

    def list_workbench_runs(self) -> list[dict[str, Any]]:
        self.initialize_runtime()
        runs: list[dict[str, Any]] = []
        for directory in self.runs_dir.iterdir():
            run_path = directory / "run.json"
            if not directory.is_dir() or not run_path.exists():
                continue
            try:
                run = self._read_json(run_path)
            except (json.JSONDecodeError, OSError, TypeError):
                continue
            if self.is_current_workbench_run(run, directory_name=directory.name):
                runs.append(run)
        return sorted(
            runs,
            key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
            reverse=True,
        )

    def is_current_workbench_run_id(self, run_id: str) -> bool:
        normalized = str(run_id or "").strip()
        if not normalized.startswith("wb-"):
            return False
        try:
            run = self.load_run(normalized)
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
            return False
        return self.is_current_workbench_run(run, directory_name=normalized)

    @staticmethod
    def _is_recognized_batch_directory_name(name: str) -> bool:
        return re.fullmatch(r"(?:wb|run)-[A-Za-z0-9][A-Za-z0-9._-]*", str(name or "")) is not None

    def archive_recognized_batch_seed_usage(self) -> dict[str, Any]:
        self.initialize_runtime()
        archived_run_ids: list[str] = []
        archived_identity_keys: set[str] = set()
        for candidate in list(self.runs_dir.iterdir()):
            if not candidate.is_dir() or not self._is_recognized_batch_directory_name(candidate.name):
                continue
            sampled_path = candidate / "sampled_seeds.json"
            if not sampled_path.exists():
                continue
            try:
                seeds = [SeedProduct.from_dict(item) for item in self._read_json(sampled_path)]
            except (json.JSONDecodeError, OSError, TypeError, ValueError, KeyError):
                continue
            if not seeds:
                continue
            self.append_used_seeds(candidate.name, seeds, "batch_cleared_after_sampling")
            archived_run_ids.append(candidate.name)
            archived_identity_keys.update(self.seed_identity_key(seed) for seed in seeds)
        return {
            "archived_seed_run_ids": sorted(archived_run_ids),
            "archived_seed_identity_count": len(archived_identity_keys),
        }

    def clear_workbench_batches(self) -> dict[str, Any]:
        self.initialize_runtime()
        runs_root = self.runs_dir.resolve(strict=False)
        deleted_run_ids: list[str] = []
        skipped_entries: list[str] = []
        with _JSON_WRITE_LOCK:
            self._reconcile_product_identity_ledger_locked()
            for candidate in list(self.runs_dir.iterdir()):
                if not candidate.is_dir() or not self._is_recognized_batch_directory_name(candidate.name):
                    continue
                resolved = candidate.resolve(strict=False)
                if resolved.parent != runs_root:
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

    def save_supplier_truth_profiles(self, run_id: str, payload: dict[str, Any]) -> Path:
        path = self.run_dir(run_id) / "supplier_truth_profiles.json"
        self._write_json(path, payload)
        return path

    def load_supplier_truth_profiles(self, run_id: str) -> dict[str, Any]:
        return self._read_json(self.run_dir(run_id) / "supplier_truth_profiles.json")

    def save_category_resolutions(self, run_id: str, payload: dict[str, Any]) -> Path:
        path = self.run_dir(run_id) / "category_resolutions.json"
        self._write_json(path, payload)
        return path

    def load_category_resolutions(self, run_id: str) -> dict[str, Any]:
        return self._read_json(self.run_dir(run_id) / "category_resolutions.json")

    def save_seller_template_cache(self, run_id: str, payload: dict[str, Any]) -> Path:
        path = self.run_dir(run_id) / "seller_template_cache.json"
        self._write_json(path, payload)
        return path

    def load_seller_template_cache(self, run_id: str) -> dict[str, Any]:
        return self._read_json(self.run_dir(run_id) / "seller_template_cache.json")

    def save_pricing_settings(self, payload: dict[str, Any]) -> Path:
        self._write_json(self.pricing_settings_path, payload)
        return self.pricing_settings_path

    def load_pricing_settings(self) -> dict[str, Any]:
        if not self.pricing_settings_path.exists():
            return PricingPolicy.default().to_dict()
        return self._read_json(self.pricing_settings_path)

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

    def image_task_quarantined_dir(self) -> Path:
        path = self.runtime_root / "image_tasks" / "quarantined"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def find_image_task_package(
        self,
        package_id: str,
    ) -> tuple[Path, dict[str, Any]] | None:
        safe_package_id = re.sub(
            r"[^A-Za-z0-9_.-]+",
            "-",
            str(package_id or "").strip(),
        ).strip(".-")
        if not safe_package_id:
            raise ValueError("Image task package_id must contain a safe filename.")
        root = self.runtime_root / "image_tasks"
        for status in (
            "pending",
            "in_progress",
            "grid_ready",
            "awaiting_product",
            "completed",
            "failed",
        ):
            path = root / status / f"{safe_package_id}.json"
            if path.is_file():
                return path, self._read_json(path)
        return None

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

    def bind_pending_image_task_product(
        self,
        package_id: str,
        *,
        run_id: str,
        seed_id: str,
        seller_import_task_id: int,
        product_id: int,
    ) -> Path:
        safe_package_id = re.sub(
            r"[^A-Za-z0-9_.-]+",
            "-",
            str(package_id or "").strip(),
        ).strip(".-")
        located = self.find_image_task_package(safe_package_id)
        if located is None:
            raise FileNotFoundError(
                self.image_task_pending_dir() / f"{safe_package_id}.json"
            )
        path, payload = located
        target = payload.get("store_target")
        if (
            str(payload.get("run_id") or "") != str(run_id)
            or str(payload.get("seed_id") or "") != str(seed_id)
            or not isinstance(target, dict)
        ):
            raise ValueError(
                "Pending image task identity does not match the accepted Ozon product."
            )
        package_status = str(payload.get("status") or path.parent.name)
        if package_status != path.parent.name:
            raise ValueError(
                "Image task package status does not match its lifecycle directory."
            )
        existing_product_id = int(target.get("product_id") or 0)
        if existing_product_id and existing_product_id != int(product_id):
            raise ValueError(
                "Claimed image task product binding does not match the accepted Ozon product."
            )
        target["seller_import_task_id"] = int(seller_import_task_id)
        target["product_id"] = int(product_id)
        target["binding_state"] = "bound"
        payload["store_target"] = target
        if package_status == "awaiting_product":
            payload["status"] = "pending"
            payload["resume_mode"] = "upload_only"
            payload.pop("assignment", None)
            destination = self.image_task_pending_dir() / path.name
            self._write_json(destination, payload)
            path.unlink(missing_ok=True)
            return destination
        self._write_json(path, payload)
        return path

    def quarantine_image_task_package(
        self,
        package_id: str,
        *,
        reason: str,
        details: dict[str, Any] | None = None,
    ) -> Path | None:
        safe_package_id = re.sub(
            r"[^A-Za-z0-9_.-]+",
            "-",
            str(package_id or "").strip(),
        ).strip(".-")
        if not safe_package_id:
            raise ValueError("Image task package_id must contain a safe filename.")
        source = self.image_task_pending_dir() / f"{safe_package_id}.json"
        if not source.is_file():
            return None
        destination = (
            self.image_task_quarantined_dir() / f"{safe_package_id}.json"
        )
        payload = self._read_json(source)
        payload.update(
            {
                "status": "quarantined",
                "quarantine_reason": str(reason),
                "quarantined_at": utc_now_iso(),
                "quarantine_details": details or {},
            }
        )
        self._write_json(destination, payload)
        source.unlink(missing_ok=True)
        return destination

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

    @staticmethod
    def _canonical_source_seed_id(value: str) -> str | None:
        normalized = str(value or "").strip().casefold().replace("_", "-")
        match = re.fullmatch(r"seed-(?:5000-)?(\d{4})", normalized)
        if match:
            return f"seed-{match.group(1)}"
        return normalized or None

    def seed_identity_key(self, seed: SeedProduct | dict[str, Any] | str) -> str:
        if isinstance(seed, SeedProduct):
            payload = seed.to_dict()
        elif isinstance(seed, dict):
            payload = dict(seed)
        else:
            payload = {"seed_id": str(seed)}

        notes = payload.get("notes")
        source_seed_id = ""
        if isinstance(notes, str) and notes.strip().startswith("{"):
            try:
                parsed_notes = json.loads(notes)
            except (json.JSONDecodeError, TypeError):
                parsed_notes = {}
            if isinstance(parsed_notes, dict):
                source_seed_id = str(parsed_notes.get("source_seed_id") or "")
        canonical = self._canonical_source_seed_id(source_seed_id or str(payload.get("seed_id") or ""))
        if canonical and re.fullmatch(r"seed-\d{4}", canonical):
            return f"source:{canonical}"

        clue = str(payload.get("product_clue") or payload.get("title_or_keyword") or "").casefold()
        normalized_clue = re.sub(r"[^0-9a-z\u0400-\u04ff\u4e00-\u9fff]+", "", clue)
        if normalized_clue:
            return f"clue:{normalized_clue}"
        return f"seed:{canonical or 'unknown'}"

    def _seed_identity_key_from_row(self, row: dict[str, Any]) -> str:
        stored = str(row.get("seed_identity_key") or "").strip()
        if stored:
            return stored
        return self.seed_identity_key(row)

    def _seed_exclusion_revision(self) -> str:
        parts: list[str] = []
        for path in (self.used_seed_path, self.seed_blacklist_path):
            try:
                stat = path.stat()
            except OSError:
                parts.append("missing")
            else:
                parts.append(f"{stat.st_size}:{stat.st_mtime_ns}")
        return "|".join(parts)

    def _reconcile_active_seed_exclusions(self) -> None:
        if not self.active_seed_path.exists():
            return
        with _JSON_WRITE_LOCK:
            payload = self._read_json(self.active_seed_path)
            revision = self._seed_exclusion_revision()
            if str(payload.get("seed_exclusion_revision") or "") == revision:
                return
            excluded = {
                self._seed_identity_key_from_row(item)
                for path in (self.used_seed_path, self.seed_blacklist_path)
                for item in self._read_jsonl(path)
                if item.get("seed_id") or item.get("seed_identity_key")
            }
            seeds = [SeedProduct.from_dict(item) for item in payload.get("seeds", [])]
            retained = [seed for seed in seeds if self.seed_identity_key(seed) not in excluded]
            payload["seeds"] = [seed.to_dict() for seed in retained]
            payload["excluded_identity_count"] = len(seeds) - len(retained)
            payload["seed_exclusion_revision"] = revision
            payload["updated_at"] = utc_now_iso()
            self._write_json(self.active_seed_path, payload)

    def remove_active_seeds(self, seed_ids: set[str]) -> list[SeedProduct]:
        seeds = self.load_active_seeds()
        removed = [seed for seed in seeds if seed.seed_id in seed_ids]
        remaining = [seed for seed in seeds if seed.seed_id not in seed_ids]
        self.save_active_seeds(remaining)
        return removed

    def remove_active_seed_identities(self, identity_keys: set[str]) -> list[SeedProduct]:
        if not identity_keys or not self.active_seed_path.exists():
            return []
        with _JSON_WRITE_LOCK:
            seeds = self.load_active_seeds()
            removed = [seed for seed in seeds if self.seed_identity_key(seed) in identity_keys]
            if removed:
                self.save_active_seeds(
                    [seed for seed in seeds if self.seed_identity_key(seed) not in identity_keys]
                )
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
