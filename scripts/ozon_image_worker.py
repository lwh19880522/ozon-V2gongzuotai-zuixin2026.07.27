from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ozon_v2.images.queue import ImageGenerationQueue  # noqa: E402
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
    claim.add_argument("--lease-seconds", type=float, default=120)

    heartbeat = commands.add_parser("heartbeat")
    heartbeat.add_argument("--job", required=True)
    heartbeat.add_argument("--worker", required=True)
    heartbeat.add_argument("--epoch", required=True, type=int)
    heartbeat.add_argument("--lease-seconds", type=float, default=120)

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

    record = commands.add_parser("record-result")
    record.add_argument("--receipt-json", required=True)

    stop = commands.add_parser("stop")
    stop.add_argument("--job", required=True)

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

    queue = _queue(args)
    if args.command == "claim":
        job = queue.claim_next(args.worker, lease_seconds=args.lease_seconds)
        _print(None if job is None else queue.snapshot(job["job_id"]))
    elif args.command == "heartbeat":
        _print(queue.heartbeat(args.job, args.worker, args.epoch, lease_seconds=args.lease_seconds))
    elif args.command == "snapshot":
        _print(queue.snapshot(args.job))
    elif args.command == "record-result":
        payload = json.loads(Path(args.receipt_json).read_text(encoding="utf-8"))
        _print(queue.record_slot_result(SlotResultReceipt.from_dict(payload)))
    elif args.command == "stop":
        queue.stop(args.job)
        _print(queue.snapshot(args.job))
    elif args.command == "resume":
        queue.resume(args.job)
        _print(queue.snapshot(args.job))
    elif args.command == "ready-for-review":
        _print(queue.mark_ready_for_review(args.job, args.worker, args.epoch))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
