from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ozon_v2.adapters.fs_repo import FsRepo  # noqa: E402
from ozon_v2.adapters.public_media import (  # noqa: E402
    CloudflareQuickTunnelMediaPublisher,
    PublicMediaError,
    stop_quick_tunnel_gateway,
)
from ozon_v2.adapters.seller_api import SellerApiAdapter, SellerApiError  # noqa: E402
from ozon_v2.app.context import build_default_context  # noqa: E402
from ozon_v2.images.slideshow import SlideshowError, build_product_slideshow  # noqa: E402
from ozon_v2.images.task_inbox import ImageTaskInbox, ImageTaskInboxError  # noqa: E402
from ozon_v2.images.worker import is_exact_three_by_four_image  # noqa: E402


def _print(payload: Any) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    try:
        print(rendered)
    except UnicodeEncodeError:
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Single-thread inbox for post-product Ozon image generation and upload tasks"
    )
    parser.add_argument("--runtime-root", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("media-start")
    commands.add_parser("media-stop")

    commands.add_parser("claim-next")

    stage = commands.add_parser("stage-grid")
    stage.add_argument("--package", required=True)
    stage.add_argument("--grid", required=True)
    stage.add_argument("--white-anchor", required=True)

    slideshow = commands.add_parser("build-slideshow")
    slideshow.add_argument("--package", required=True)
    slideshow.add_argument("--output-dir", required=True)
    slideshow.add_argument(
        "--image",
        action="append",
        required=True,
        help="Ordered slot_id=absolute_path entry; provide exactly eight.",
    )

    stage_media = commands.add_parser("stage-media")
    stage_media.add_argument("--package", required=True)
    stage_media.add_argument(
        "--image",
        action="append",
        required=True,
        help="Ordered slot_id=absolute_path entry; provide exactly eight.",
    )
    stage_media.add_argument("--video", required=True)
    stage_media.add_argument("--video-cover", required=True)

    upload = commands.add_parser("upload-gallery")
    upload.add_argument("--package", required=True)
    upload.add_argument("--product-id", type=int)
    upload.add_argument(
        "--image",
        action="append",
        help="Ordered slot_id=absolute_path entry; provide exactly eight.",
    )
    upload.add_argument("--video")
    upload.add_argument("--video-cover")

    fail = commands.add_parser("fail")
    fail.add_argument("--package", required=True)
    fail.add_argument("--reason", required=True)

    release = commands.add_parser("release")
    release.add_argument("--package", required=True)
    release.add_argument("--reason", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    runtime_root = Path(args.runtime_root).resolve()
    inbox = ImageTaskInbox(runtime_root)
    context = build_default_context(runtime_root=runtime_root)
    repo = FsRepo(context)
    publisher = CloudflareQuickTunnelMediaPublisher(runtime_root)
    if args.command == "status":
        _print(inbox.status())
        return 0
    if args.command == "media-start":
        _print(publisher.preflight())
        return 0
    if args.command == "media-stop":
        _print(stop_quick_tunnel_gateway(runtime_root))
        return 0
    if args.command == "claim-next":
        _print(inbox.claim_next() or {"status": "idle"})
        return 0
    if args.command == "fail":
        _print(inbox.fail(args.package, args.reason))
        return 0
    if args.command == "release":
        _print(inbox.release(args.package, args.reason))
        return 0
    if args.command == "stage-grid":
        package = inbox.load_in_progress(args.package)
        phase = str((package.get("assignment") or {}).get("phase") or "")
        if phase != "grid_generation":
            raise ImageTaskInboxError(
                f"Package {args.package} is not assigned for grid generation."
            )
        grid_path = _media_path(args.grid, (".jpg", ".jpeg", ".png", ".webp"), "4x2 grid")
        anchor_path = _media_path(
            args.white_anchor,
            (".jpg", ".jpeg", ".png", ".webp"),
            "white identity anchor",
        )
        if not _is_exact_three_by_two_image(grid_path):
            raise ImageTaskInboxError(f"Raw 4x2 grid is not exact 3:2: {grid_path}")
        if not is_exact_three_by_four_image(anchor_path):
            raise ImageTaskInboxError(
                f"White identity anchor is not exact 3:4: {anchor_path}"
            )
        staged = inbox.stage_grid(
            args.package,
            {
                "raw_grid_path": str(grid_path),
                "raw_grid_sha256": _sha256(grid_path),
                "white_anchor_path": str(anchor_path),
                "white_anchor_sha256": _sha256(anchor_path),
            },
        )
        _print(staged)
        return 0

    package = inbox.load_in_progress(args.package)
    phase = str((package.get("assignment") or {}).get("phase") or "")
    if args.command in {"build-slideshow", "stage-media"} and phase != "grid_crop":
        raise ImageTaskInboxError(
            f"Package {args.package} is not assigned for product media completion."
        )
    if args.command == "upload-gallery" and phase != "upload_only":
        raise ImageTaskInboxError(
            f"Package {args.package} cannot upload before the batch media barrier."
        )
    if args.command == "upload-gallery" and not args.image:
        generated_media = package.get("generated_media") or {}
        args.image = [
            f"{item.get('slot_id')}={item.get('path')}"
            for item in generated_media.get("images") or []
            if isinstance(item, dict)
        ]
        args.video = args.video or generated_media.get("video")
        args.video_cover = args.video_cover or generated_media.get("video_cover")
    source_files = _ordered_source_files(args.image)
    if args.command == "build-slideshow":
        result = build_product_slideshow(
            [item["path"] for item in source_files],
            output_dir=Path(args.output_dir).resolve(),
        )
        _print(result)
        return 0

    video_path = _media_path(args.video, ".mp4", "slideshow video")
    cover_path = _media_path(
        args.video_cover,
        (".jpg", ".jpeg"),
        "slideshow video cover",
    )
    if not is_exact_three_by_four_image(cover_path):
        raise ImageTaskInboxError(
            f"Slideshow video cover is not exact 3:4: {cover_path}"
        )
    if args.command == "stage-media":
        staged = inbox.stage_media(
            args.package,
            {
                "images": source_files,
                "video": str(video_path),
                "video_cover": str(cover_path),
            },
        )
        _print(staged)
        return 0
    seller_api = SellerApiAdapter(repo)
    try:
        product_id = int(args.product_id or _resolve_product_id(package, seller_api))
    except SellerApiError:
        waiting = inbox.await_product(
            args.package,
            {
                "images": source_files,
                "video": str(video_path),
                "video_cover": str(cover_path),
            },
        )
        _print(waiting)
        return 0
    publication = publisher.publish_product(
        run_id=str(package["run_id"]),
        seed_id=str(package["seed_id"]),
        source_files=[
            *source_files,
            {"slot_id": "slideshow_video", "path": str(video_path)},
            {"slot_id": "video_cover", "path": str(cover_path)},
        ],
    )
    public_by_slot = {
        str(item["slot_id"]): str(item["public_url"])
        for item in publication["items"]
    }
    gallery_urls = [
        public_by_slot[str(item["slot_id"])]
        for item in source_files
    ]
    picture_result = seller_api.replace_product_pictures(
        product_id=product_id,
        images=gallery_urls,
    )
    target = package.get("store_target") or {}
    video_result = seller_api.attach_product_video_assets(
        seller_api_item=target.get("seller_api_item") or {},
        video_url=public_by_slot["slideshow_video"],
        video_cover_url=public_by_slot["video_cover"],
        video_template_fields=target.get("video_template_fields") or [],
        image_urls=gallery_urls,
    )
    completed = inbox.complete(
        args.package,
        {
            "product_id": product_id,
            "public_media": publication,
            "ozon_picture_import": picture_result,
            "ozon_video_import": video_result,
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


def _media_path(
    value: str,
    suffixes: str | tuple[str, ...],
    label: str,
) -> Path:
    path = Path(str(value or "")).resolve()
    accepted = (suffixes,) if isinstance(suffixes, str) else suffixes
    if not path.is_file() or path.suffix.casefold() not in accepted:
        raise ImageTaskInboxError(f"Invalid {label}: {path}")
    return path


def _is_exact_three_by_two_image(path: Path) -> bool:
    try:
        with Image.open(path) as image:
            width, height = image.size
    except (OSError, UnidentifiedImageError):
        return False
    return width > 0 and height > 0 and width * 2 == height * 3


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_product_id(
    package: dict[str, Any],
    seller_api: SellerApiAdapter,
) -> int:
    target = package.get("store_target") or {}
    stored_product_id = target.get("product_id")
    if stored_product_id:
        return int(stored_product_id)
    offer_id = str(target.get("offer_id") or "").strip()
    lookup = getattr(seller_api, "get_product_state_by_offer_id", None)
    if offer_id and callable(lookup):
        product_state = lookup(offer_id)
        if (
            isinstance(product_state, dict)
            and product_state.get("is_created") is True
            and int(product_state.get("product_id") or 0) > 0
        ):
            return int(product_state["product_id"])
    task_id = target.get("seller_import_task_id")
    if task_id is None:
        raise SellerApiError("Image task package has no Seller import task ID.")
    status = seller_api.get_product_import_info(int(task_id))
    for item in status.get("items") or []:
        if offer_id and str(item.get("offer_id") or "") != offer_id:
            continue
        product_id = item.get("product_id")
        item_status = str(item.get("status") or "").strip().casefold()
        created = item.get("is_created") is True or item_status in {
            "accepted",
            "created",
            "imported",
            "processed",
            "success",
        }
        if product_id and created:
            return int(product_id)
    raise SellerApiError("Ozon product_id is not ready for this image task package.")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        ImageTaskInboxError,
        PublicMediaError,
        SellerApiError,
        SlideshowError,
    ) as exc:
        _print({"ok": False, "error": str(exc)})
        raise SystemExit(2)
