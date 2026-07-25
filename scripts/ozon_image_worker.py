from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ozon_v2.images.queue import (  # noqa: E402
    DEFAULT_IMAGE_LEASE_SECONDS,
    ImageGenerationQueue,
)
from ozon_v2.images.runtime_assets import (  # noqa: E402
    load_generation_checkpoints,
    materialize_ozon_references,
    ozon_gallery_urls_for_product,
    persist_generation_checkpoint,
)
from ozon_v2.images.visual_design import VisualSpec, render_visual  # noqa: E402
from ozon_v2.images.worker import SlotResultReceipt, crop_grid  # noqa: E402


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _queue(args: argparse.Namespace) -> ImageGenerationQueue:
    return ImageGenerationQueue(args.db)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ozon V2 local image worker queue control")
    parser.add_argument("--db", required=True, help="Absolute path to the image queue SQLite file")
    commands = parser.add_subparsers(dest="command", required=True)

    claim = commands.add_parser("claim")
    claim.add_argument("--worker", required=True)
    claim.add_argument(
        "--lease-seconds", type=float, default=DEFAULT_IMAGE_LEASE_SECONDS
    )

    claim_assigned = commands.add_parser("claim-assigned")
    claim_assigned.add_argument("--worker", required=True)
    claim_assigned.add_argument("--job", required=True)
    claim_assigned.add_argument("--instruction-id", required=True)
    claim_assigned.add_argument(
        "--lease-seconds", type=float, default=DEFAULT_IMAGE_LEASE_SECONDS
    )

    heartbeat = commands.add_parser("heartbeat")
    heartbeat.add_argument("--job", required=True)
    heartbeat.add_argument("--worker", required=True)
    heartbeat.add_argument("--epoch", required=True, type=int)
    heartbeat.add_argument(
        "--lease-seconds", type=float, default=DEFAULT_IMAGE_LEASE_SECONDS
    )

    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--job", required=True)

    crop = commands.add_parser("crop-grid")
    crop.add_argument("--source", required=True)
    crop.add_argument("--output-dir", required=True)
    crop.add_argument("--layout", required=True, choices=("1x2", "1x3"))
    crop.add_argument("--basename", required=True)

    render = commands.add_parser("render-visual")
    render.add_argument("--source", required=True)
    render.add_argument("--output", required=True)
    render.add_argument("--spec-json", required=True)
    render.add_argument("--evidence-sha256", action="append", required=True)

    references = commands.add_parser("materialize-references")
    references.add_argument("--collection-json", required=True)
    references.add_argument("--product-id", required=True)
    references.add_argument("--output-dir", required=True)
    references.add_argument("--minimum-valid", type=int, default=4)

    checkpoint = commands.add_parser("checkpoint-asset")
    checkpoint.add_argument("--job", required=True)
    checkpoint.add_argument("--worker", required=True)
    checkpoint.add_argument("--epoch", required=True, type=int)
    checkpoint.add_argument("--kind", required=True)
    checkpoint.add_argument("--source", required=True)
    checkpoint.add_argument("--checkpoint-dir", required=True)
    checkpoint.add_argument("--layout", choices=("1x2", "1x3"))
    checkpoint.add_argument("--basename")
    checkpoint.add_argument(
        "--lease-seconds", type=float, default=DEFAULT_IMAGE_LEASE_SECONDS
    )

    checkpoint_status = commands.add_parser("checkpoint-status")
    checkpoint_status.add_argument("--job", required=True)
    checkpoint_status.add_argument("--checkpoint-dir", required=True)

    record = commands.add_parser("record-result")
    record.add_argument("--receipt-json", required=True)

    stop = commands.add_parser("stop")
    stop.add_argument("--job", required=True)
    stop.add_argument("--reason", required=True)
    stop.add_argument("--stopped-by", default="image_worker")

    resume = commands.add_parser("resume")
    resume.add_argument("--job", required=True)

    finalize = commands.add_parser("ready-for-review")
    finalize.add_argument("--job", required=True)
    finalize.add_argument("--worker", required=True)
    finalize.add_argument("--epoch", required=True, type=int)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "render-visual":
        payload = json.loads(Path(args.spec_json).read_text(encoding="utf-8"))
        _print(
            render_visual(
                args.source,
                args.output,
                VisualSpec.from_dict(payload),
                set(args.evidence_sha256),
            )
        )
        return 0
    if args.command == "crop-grid":
        outputs = crop_grid(args.source, args.output_dir, layout=args.layout, basename=args.basename)
        _print({"outputs": [str(path.resolve()) for path in outputs]})
        return 0
    if args.command == "materialize-references":
        urls = ozon_gallery_urls_for_product(
            args.collection_json,
            args.product_id,
        )
        _print(
            materialize_ozon_references(
                urls,
                args.output_dir,
                minimum_valid=args.minimum_valid,
            )
        )
        return 0
    if args.command == "checkpoint-status":
        _print(load_generation_checkpoints(args.job, args.checkpoint_dir))
        return 0

    queue = _queue(args)
    if args.command == "claim":
        job = queue.claim_next(args.worker, lease_seconds=args.lease_seconds)
        _print(None if job is None else queue.snapshot(job["job_id"]))
    elif args.command == "claim-assigned":
        job = queue.claim_assigned(
            args.worker,
            args.job,
            args.instruction_id,
            lease_seconds=args.lease_seconds,
        )
        _print(queue.snapshot(job["job_id"]))
    elif args.command == "heartbeat":
        _print(queue.heartbeat(args.job, args.worker, args.epoch, lease_seconds=args.lease_seconds))
    elif args.command == "checkpoint-asset":
        queue.heartbeat(
            args.job,
            args.worker,
            args.epoch,
            lease_seconds=args.lease_seconds,
        )
        _print(
            persist_generation_checkpoint(
                job_id=args.job,
                asset_kind=args.kind,
                source_path=args.source,
                checkpoint_dir=args.checkpoint_dir,
                layout=args.layout,
                basename=args.basename,
            )
        )
    elif args.command == "snapshot":
        _print(queue.snapshot(args.job))
    elif args.command == "record-result":
        payload = json.loads(Path(args.receipt_json).read_text(encoding="utf-8"))
        _print(queue.record_slot_result(SlotResultReceipt.from_dict(payload)))
    elif args.command == "stop":
        queue.stop(args.job, reason=args.reason, stopped_by=args.stopped_by)
        _print(queue.snapshot(args.job))
    elif args.command == "resume":
        queue.resume(args.job)
        _print(queue.snapshot(args.job))
    elif args.command == "ready-for-review":
        _print(queue.mark_ready_for_review(args.job, args.worker, args.epoch))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
