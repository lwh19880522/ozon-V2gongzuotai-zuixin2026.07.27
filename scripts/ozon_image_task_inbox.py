from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ozon_v2.adapters.fs_repo import FsRepo  # noqa: E402
from ozon_v2.adapters.public_media import CloudflareR2MediaPublisher  # noqa: E402
from ozon_v2.adapters.seller_api import SellerApiAdapter, SellerApiError  # noqa: E402
from ozon_v2.app.context import build_default_context  # noqa: E402
from ozon_v2.images.task_inbox import ImageTaskInbox, ImageTaskInboxError  # noqa: E402
from ozon_v2.images.worker import is_exact_three_by_four_image  # noqa: E402


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fixed inbox for post-product Ozon image generation and upload tasks"
    )
    parser.add_argument("--runtime-root", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")

    claim = commands.add_parser("claim-next")
    claim.add_argument("--worker", required=True)

    upload = commands.add_parser("upload-gallery")
    upload.add_argument("--package", required=True)
    upload.add_argument("--worker", required=True)
    upload.add_argument("--product-id", type=int)
    upload.add_argument(
        "--image",
        action="append",
        required=True,
        help="Ordered slot_id=absolute_path entry; provide exactly eight.",
    )

    fail = commands.add_parser("fail")
    fail.add_argument("--package", required=True)
    fail.add_argument("--worker", required=True)
    fail.add_argument("--reason", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    runtime_root = Path(args.runtime_root).resolve()
    inbox = ImageTaskInbox(runtime_root)
    if args.command == "status":
        _print(inbox.status())
        return 0
    if args.command == "claim-next":
        _print(inbox.claim_next(args.worker) or {"status": "idle"})
        return 0
    if args.command == "fail":
        _print(inbox.fail(args.package, args.worker, args.reason))
        return 0

    package = inbox.load_in_progress(args.package, args.worker)
    source_files = _ordered_source_files(args.image)
    context = build_default_context(runtime_root=runtime_root)
    repo = FsRepo(context)
    seller_api = SellerApiAdapter(repo)
    product_id = int(args.product_id or _resolve_product_id(package, seller_api))
    publication = CloudflareR2MediaPublisher().publish_product(
        run_id=str(package["run_id"]),
        seed_id=str(package["seed_id"]),
        source_files=source_files,
        settings=repo.load_public_media_settings(),
    )
    picture_result = seller_api.replace_product_pictures(
        product_id=product_id,
        images=list(publication["urls"]),
    )
    completed = inbox.complete(
        args.package,
        args.worker,
        {
            "product_id": product_id,
            "public_media": publication,
            "ozon_picture_import": picture_result,
        },
    )
    _print(completed)
    return 0


def _ordered_source_files(values: list[str]) -> list[dict[str, Any]]:
    source_files: list[dict[str, Any]] = []
    for value in values:
        slot_id, separator, raw_path = str(value).partition("=")
        path = Path(raw_path).resolve()
        if not separator or not slot_id.strip() or not path.is_file():
            raise ImageTaskInboxError(f"Invalid generated image entry: {value}")
        if not is_exact_three_by_four_image(path):
            raise ImageTaskInboxError(f"Generated image is not exact 3:4: {path}")
        source_files.append({"slot_id": slot_id.strip(), "path": str(path)})
    if len(source_files) != 8:
        raise ImageTaskInboxError("Exactly eight ordered generated images are required.")
    if len({item["slot_id"] for item in source_files}) != 8:
        raise ImageTaskInboxError("Generated image slot IDs must be unique.")
    return source_files


def _resolve_product_id(
    package: dict[str, Any],
    seller_api: SellerApiAdapter,
) -> int:
    target = package.get("store_target") or {}
    stored_product_id = target.get("product_id")
    if stored_product_id:
        return int(stored_product_id)
    task_id = target.get("seller_import_task_id")
    if task_id is None:
        raise SellerApiError("Image task package has no Seller import task ID.")
    status = seller_api.get_product_import_info(int(task_id))
    offer_id = str(target.get("offer_id") or "")
    for item in status.get("items") or []:
        if offer_id and str(item.get("offer_id") or "") != offer_id:
            continue
        product_id = item.get("product_id")
        if product_id:
            return int(product_id)
    raise SellerApiError("Ozon product_id is not ready for this image task package.")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ImageTaskInboxError, SellerApiError) as exc:
        _print({"ok": False, "error": str(exc)})
        raise SystemExit(2)
