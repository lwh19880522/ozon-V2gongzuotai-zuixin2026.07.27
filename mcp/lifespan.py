from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from ozon_v2.app.context import AppContext, build_default_context


@contextmanager
def app_lifespan() -> AppContext:
    """Create one request-independent AppContext for the MCP process lifetime."""
    context = build_default_context()
    lock_path = context.runtime_root / "state" / "ozon_v2.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("ozon-v2\n", encoding="utf-8")
    try:
        yield context
    finally:
        try:
            Path(lock_path).unlink(missing_ok=True)
        except OSError:
            pass

