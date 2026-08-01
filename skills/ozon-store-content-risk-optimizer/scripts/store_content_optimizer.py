from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ozon_v2.adapters.fs_repo import FsRepo
from ozon_v2.adapters.seller_api import SellerApiAdapter


RULE_VERSION = "1.0.0"
MEDIA_KEYS = frozenset(
    {"images", "primary_image", "images360", "video", "video_cover"}
)
TERMINAL_UNCHANGED_STATUSES = frozenset(
    {"completed", "pending_risk", "evidence_insufficient", "paused"}
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def store_key(client_id: str) -> str:
    return hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:16]


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def payload_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def source_fingerprint(product: dict[str, Any]) -> str:
    tracked = {
        key: product.get(key)
        for key in (
            "product_id",
            "sku",
            "offer_id",
            "visibility",
            "name",
            "description",
            "rich_content",
            "attributes",
            "images",
            "primary_image",
            "price",
            "stocks",
            "description_category_id",
            "type_id",
        )
    }
    return payload_hash(tracked)


def non_media_score(groups: dict[str, dict[str, float]]) -> int | None:
    included = [
        value
        for key, value in groups.items()
        if key.casefold() != "media" and isinstance(value, dict)
    ]
    maximum = sum(float(item.get("maximum") or 0) for item in included)
    if maximum <= 0:
        return None
    earned = sum(float(item.get("earned") or 0) for item in included)
    return round(100 * earned / maximum)


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS product_state (
                product_id TEXT PRIMARY KEY,
                sku TEXT NOT NULL,
                offer_id TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL,
                rule_hashes_json TEXT NOT NULL,
                task_json TEXT NOT NULL,
                status TEXT NOT NULL,
                risk_level TEXT,
                risk_codes_json TEXT NOT NULL DEFAULT '[]',
                proposal_fingerprint TEXT,
                original_snapshot_json TEXT,
                last_error TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS action_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id TEXT NOT NULL,
                action TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                result TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(product_id, action, payload_hash)
            );
            """
        )
        self.connection.commit()

    def get_state(self, product_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM product_state WHERE product_id = ?",
            (str(product_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def upsert_task(
        self,
        task: dict[str, Any],
        rule_hashes: dict[str, str],
    ) -> None:
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO product_state (
                product_id, sku, offer_id, source_fingerprint,
                rule_hashes_json, task_json, status, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)
            ON CONFLICT(product_id) DO UPDATE SET
                sku = excluded.sku,
                offer_id = excluded.offer_id,
                source_fingerprint = excluded.source_fingerprint,
                rule_hashes_json = excluded.rule_hashes_json,
                task_json = excluded.task_json,
                status = 'queued',
                risk_level = NULL,
                risk_codes_json = '[]',
                proposal_fingerprint = NULL,
                last_error = NULL,
                updated_at = excluded.updated_at
            """,
            (
                str(task["product_id"]),
                str(task.get("sku") or ""),
                str(task.get("offer_id") or ""),
                str(task["source_fingerprint"]),
                canonical_json(rule_hashes),
                canonical_json(task),
                now,
            ),
        )
        self.connection.commit()

    def next_task(self) -> dict[str, Any] | None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT product_id, task_json
                FROM product_state
                WHERE status = 'queued'
                ORDER BY updated_at, product_id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                self.connection.commit()
                return None
            self.connection.execute(
                "UPDATE product_state SET status = 'drafting', updated_at = ? WHERE product_id = ?",
                (utc_now(), str(row["product_id"])),
            )
            self.connection.commit()
            return json.loads(str(row["task_json"]))
        except Exception:
            self.connection.rollback()
            raise

    def mark_completed(
        self,
        product_id: str,
        fingerprint: str,
        proposal_fingerprint: str,
    ) -> None:
        self.connection.execute(
            """
            UPDATE product_state
            SET status = 'completed', source_fingerprint = ?,
                proposal_fingerprint = ?, last_error = NULL, updated_at = ?
            WHERE product_id = ?
            """,
            (fingerprint, proposal_fingerprint, utc_now(), str(product_id)),
        )
        self.connection.commit()

    def mark_status(
        self,
        product_id: str,
        status: str,
        *,
        risk_level: str | None = None,
        risk_codes: list[str] | None = None,
        error: str | None = None,
    ) -> None:
        self.connection.execute(
            """
            UPDATE product_state
            SET status = ?, risk_level = ?, risk_codes_json = ?,
                last_error = ?, updated_at = ?
            WHERE product_id = ?
            """,
            (
                status,
                risk_level,
                canonical_json(risk_codes or []),
                error,
                utc_now(),
                str(product_id),
            ),
        )
        self.connection.commit()

    def save_original(self, product_id: str, original: dict[str, Any]) -> None:
        self.connection.execute(
            """
            UPDATE product_state
            SET original_snapshot_json = ?, status = 'applying', updated_at = ?
            WHERE product_id = ?
            """,
            (canonical_json(original), utc_now(), str(product_id)),
        )
        self.connection.commit()

    def log_action(
        self,
        product_id: str,
        action: str,
        request_payload: Any,
        result: str,
    ) -> bool:
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO action_log (
                product_id, action, payload_hash, result, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                str(product_id),
                action,
                payload_hash(request_payload),
                result,
                utc_now(),
            ),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def status_summary(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT status, COUNT(*) AS count FROM product_state GROUP BY status"
        ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}


def _is_archived(product: dict[str, Any]) -> bool:
    return bool(product.get("archived")) or str(
        product.get("visibility") or ""
    ).upper() == "ARCHIVED"


def _build_task(
    product: dict[str, Any],
    evidence_bundle: dict[str, Any],
) -> dict[str, Any]:
    fingerprint = source_fingerprint(product)
    images = [str(value) for value in product.get("images") or [] if value]
    product_id = str(product.get("product_id") or product.get("id") or "")
    return {
        "product_id": product_id,
        "sku": str(product.get("sku") or ""),
        "offer_id": str(product.get("offer_id") or ""),
        "source_fingerprint": fingerprint,
        "visibility": str(product.get("visibility") or ""),
        "seller_api_item": product.get("seller_api_item") or dict(product),
        "current": {
            "name": str(product.get("name") or ""),
            "description": str(product.get("description") or ""),
            "rich_content": product.get("rich_content") or {},
            "attributes": product.get("attributes") or [],
        },
        "attribute_schema": product.get("attribute_schema") or [],
        "objective_evidence": evidence_bundle.get("objective_evidence") or {},
        "read_only_images": images,
        "product_url": str(
            product.get("product_url")
            or f"https://www.ozon.ru/product/{product_id}/"
        ),
        "storefront_facts": product.get("storefront_facts") or {},
        "pricing_evidence": evidence_bundle.get("pricing_evidence") or {},
        "evidence_insufficient": bool(
            evidence_bundle.get("evidence_insufficient", True)
        ),
        "non_media_score": non_media_score(
            product.get("content_score_groups") or {}
        ),
    }


def scan_store(
    gateway: Any,
    ledger: Ledger,
    evidence: Any,
    rule_hashes: dict[str, str],
    *,
    force: bool = False,
) -> dict[str, int]:
    counts = {
        "queued": 0,
        "skipped_archived": 0,
        "skipped_unchanged": 0,
    }
    for product in gateway.fetch_catalog():
        if _is_archived(product):
            counts["skipped_archived"] += 1
            continue
        product_id = str(product.get("product_id") or product.get("id") or "")
        if not product_id:
            continue
        bundle = evidence.for_offer(str(product.get("offer_id") or ""))
        task = _build_task(product, bundle)
        current = ledger.get_state(product_id)
        unchanged = bool(
            current
            and str(current["source_fingerprint"]) == task["source_fingerprint"]
            and json.loads(str(current["rule_hashes_json"])) == rule_hashes
        )
        if (
            not force
            and unchanged
            and str(current["status"]) in TERMINAL_UNCHANGED_STATUSES
        ):
            counts["skipped_unchanged"] += 1
            continue
        ledger.upsert_task(task, rule_hashes)
        counts["queued"] += 1
    return counts


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _exact_offer_record(payload: Any, offer_id: str) -> dict[str, Any] | None:
    target = offer_id.strip().casefold()
    if not target:
        return None
    for item in _walk_dicts(payload):
        candidate = str(item.get("offer_id") or "").strip().casefold()
        if candidate == target:
            return item
    return None


def _has_locked_supplier_sku(payload: Any) -> bool:
    for item in _walk_dicts(payload):
        if item.get("locked_supplier_sku") is True or item.get("locked") is True:
            return True
        status = str(
            item.get("selection_status") or item.get("status") or ""
        ).casefold()
        if status in {"confirmed", "locked", "selected"} and any(
            key in item for key in ("supplier_sku", "sku_id", "sku")
        ):
            return True
    return False


class WorkbenchEvidence:
    def __init__(self, repo: FsRepo | None = None) -> None:
        self.repo = repo or FsRepo()

    def _safe_load(self, method_name: str, run_id: str) -> dict[str, Any]:
        try:
            payload = getattr(self.repo, method_name)(run_id)
        except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def for_offer(self, offer_id: str) -> dict[str, Any]:
        for run in self.repo.list_runs():
            if str(run.get("kind") or "") != "workbench_batch":
                continue
            run_id = str(run.get("run_id") or "")
            if not run_id:
                continue
            submissions = self._safe_load("load_upload_submissions", run_id)
            previews = self._safe_load("load_upload_previews", run_id)
            matched_submission = _exact_offer_record(submissions, offer_id)
            matched_preview = _exact_offer_record(previews, offer_id)
            if not matched_submission and not matched_preview:
                continue
            selections = self._safe_load("load_supplier_sku_selections", run_id)
            objective = self._safe_load(
                "load_required_attribute_evidence", run_id
            )
            pricing = self._safe_load("load_pricing_evidence", run_id)
            locked = _has_locked_supplier_sku(selections)
            return {
                "offer_id": offer_id,
                "run_id": run_id,
                "locked_supplier_sku": locked,
                "evidence_insufficient": not locked,
                "objective_evidence": objective,
                "pricing_evidence": pricing,
                "upload_submission": matched_submission or {},
                "upload_preview": matched_preview or {},
                "supplier_selection": selections if locked else {},
            }
        return {
            "offer_id": offer_id,
            "locked_supplier_sku": False,
            "evidence_insufficient": True,
            "objective_evidence": {},
            "pricing_evidence": {},
        }


def _chunks(values: list[int], size: int) -> list[list[int]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _normalize_visibility(ref: dict[str, Any], info: dict[str, Any]) -> str:
    if ref.get("archived") or info.get("archived"):
        return "ARCHIVED"
    raw = str(
        ref.get("visibility")
        or info.get("visibility")
        or (info.get("statuses") or {}).get("status_name")
        or ""
    ).strip()
    folded = raw.casefold()
    if any(token in folded for token in ("прода", "in_sale", "on_sale")):
        return "IN_SALE"
    if any(token in folded for token in ("готов", "ready_to_supply", "готов к продаже")):
        return "READY_TO_SUPPLY"
    if ref.get("has_fbs_stocks") or ref.get("has_fbo_stocks"):
        return "IN_SALE"
    return raw.upper().replace(" ", "_") or "NOT_IN_SALE"


class SellerGateway:
    def __init__(self, adapter: SellerApiAdapter | None = None) -> None:
        self.adapter = adapter or SellerApiAdapter()

    def _fetch_attributes(self, product_ids: list[int]) -> dict[str, dict[str, Any]]:
        by_id: dict[str, dict[str, Any]] = {}
        for chunk in _chunks(product_ids, 100):
            payload = self.adapter._post_json(
                "/v4/product/info/attributes",
                {"filter": {"product_id": chunk}, "limit": 100},
            )
            result = payload.get("result", payload)
            items = result.get("items", []) if isinstance(result, dict) else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                product_id = str(item.get("id") or item.get("product_id") or "")
                if product_id:
                    by_id[product_id] = item
        return by_id

    def fetch_catalog(self) -> list[dict[str, Any]]:
        refs = self.adapter._fetch_product_refs()
        product_ids = [int(item["product_id"]) for item in refs]
        info_by_id = self.adapter._fetch_product_info(product_ids)
        attributes_by_id = self._fetch_attributes(product_ids)
        products: list[dict[str, Any]] = []
        for ref in refs:
            product_id = str(ref["product_id"])
            info = info_by_id.get(product_id, {})
            attributes = attributes_by_id.get(product_id, {})
            merged = {**info, **attributes}
            images = merged.get("images") or []
            seller_item = dict(attributes or merged)
            seller_item.setdefault("offer_id", ref.get("offer_id"))
            products.append(
                {
                    **merged,
                    "product_id": product_id,
                    "sku": str(ref.get("sku") or merged.get("sku") or ""),
                    "offer_id": str(
                        ref.get("offer_id") or merged.get("offer_id") or ""
                    ),
                    "visibility": _normalize_visibility(ref, merged),
                    "archived": bool(ref.get("archived")),
                    "name": str(merged.get("name") or ""),
                    "description": str(merged.get("description") or ""),
                    "rich_content": merged.get("rich_content") or {},
                    "attributes": merged.get("attributes") or [],
                    "images": images,
                    "primary_image": merged.get("primary_image")
                    or (images[0] if images else None),
                    "price": merged.get("price"),
                    "stocks": merged.get("stocks") or [],
                    "description_category_id": merged.get(
                        "description_category_id"
                    ),
                    "type_id": merged.get("type_id"),
                    "content_score_groups": merged.get(
                        "content_score_groups"
                    )
                    or merged.get("content_rating")
                    or {},
                    "seller_api_item": seller_item,
                }
            )
        return products


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("scan")
    subparsers.add_parser("next")
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--proposal", default="-")
    subparsers.add_parser("status")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload: dict[str, Any] = {"ok": True, "command": args.command}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
