from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ozon_v2.adapters.public_media import serve_quick_tunnel_gateway  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Automatic temporary public media gateway for Ozon uploads"
    )
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("command", choices=("serve",))
    args = parser.parse_args()
    serve_quick_tunnel_gateway(Path(args.runtime_root).resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
