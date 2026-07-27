from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ozon_v2.images.queue import ImageGenerationQueue  # noqa: E402


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Lightweight dispatcher for ten fixed Ozon image tasks"
    )
    parser.add_argument("--db", required=True, help="Absolute image queue SQLite path")
    commands = parser.add_subparsers(dest="command", required=True)

    register = commands.add_parser("register")
    register.add_argument("--worker", required=True)
    register.add_argument("--thread-id", required=True)
    register.add_argument("--skill-sha256", default="")
    register.add_argument("--replace", action="store_true")

    dispatch = commands.add_parser("dispatch")
    dispatch.add_argument("--run")
    dispatch.add_argument("--limit", type=int, default=10)

    commands.add_parser("status")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    queue = ImageGenerationQueue(args.db)
    if args.command == "register":
        _print(
            queue.register_worker_slot(
                args.worker,
                args.thread_id,
                skill_sha256=args.skill_sha256,
                replace=args.replace,
            )
        )
        return 0
    if args.command == "status":
        slots = queue.list_worker_slots()
        _print(
            {
                "slots": slots,
                "summary": {
                    "registered": len(slots),
                    "idle": sum(slot["status"] == "idle" for slot in slots),
                    "assigned": sum(slot["status"] == "assigned" for slot in slots),
                },
            }
        )
        return 0

    assignments = queue.dispatch_assignments(
        run_id=args.run,
        limit=args.limit,
    )
    slots = queue.list_worker_slots()
    _print(
        {
            "assignments": assignments,
            "summary": {
                "assigned": len(assignments),
                "registered": len(slots),
                "active": sum(slot["status"] == "assigned" for slot in slots),
            },
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
