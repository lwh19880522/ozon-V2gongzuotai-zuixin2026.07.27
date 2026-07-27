from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

try:
    from fastmcp import FastMCP
except ImportError:  # pragma: no cover - used only when FastMCP is unavailable locally.
    class FastMCP:  # type: ignore[no-redef]
        def __init__(self, name: str, lifespan=None) -> None:
            self.name = name
            self.lifespan = lifespan
            self.tools = {}
            self.resources = {}

        def tool(self, name: str | None = None):
            def decorator(func):
                self.tools[name or func.__name__] = func
                return func

            return decorator

        def resource(self, uri: str):
            def decorator(func):
                self.resources[uri] = func
                return func

            return decorator

        def run(self) -> None:
            raise RuntimeError("FastMCP is not installed in this Python environment.")

from mcp.lifespan import app_lifespan
from ozon_v2.resources.artifacts import get_artifacts_resource
from ozon_v2.resources.run_status import get_run_status_resource
from ozon_v2.tools.credentials import (
    ozon_v2_credentials_status,
    ozon_v2_credentials_template,
    ozon_v2_open_credential_assistant,
    ozon_v2_save_credentials,
)
from ozon_v2.tools.dedupe import ozon_v2_refresh_existing_store_dedupe
from ozon_v2.tools.doctor import ozon_v2_doctor
from ozon_v2.tools.ingest import ozon_v2_ingest_collection_output
from ozon_v2.tools.runs import ozon_v2_next, ozon_v2_replace_sampled_seed, ozon_v2_start_run, ozon_v2_status
from ozon_v2.tools.workbench import (
    ozon_v2_workbench_allowed_actions,
    ozon_v2_workbench_dispatch,
    ozon_v2_workbench_start_batch,
)


def create_app() -> FastMCP:
    mcp = FastMCP("ozon-v2", lifespan=app_lifespan)

    mcp.tool("ozon_v2_doctor")(ozon_v2_doctor)
    mcp.tool("ozon_v2_credentials_template")(ozon_v2_credentials_template)
    mcp.tool("ozon_v2_credentials_status")(ozon_v2_credentials_status)
    mcp.tool("ozon_v2_open_credential_assistant")(ozon_v2_open_credential_assistant)
    mcp.tool("ozon_v2_save_credentials")(ozon_v2_save_credentials)
    mcp.tool("ozon_v2_refresh_existing_store_dedupe")(ozon_v2_refresh_existing_store_dedupe)
    mcp.tool("ozon_v2_status")(ozon_v2_status)
    mcp.tool("ozon_v2_start_run")(ozon_v2_start_run)
    mcp.tool("ozon_v2_replace_sampled_seed")(ozon_v2_replace_sampled_seed)
    mcp.tool("ozon_v2_next")(ozon_v2_next)
    mcp.tool("ozon_v2_ingest_collection_output")(ozon_v2_ingest_collection_output)
    mcp.tool("ozon_v2_workbench_start_batch")(ozon_v2_workbench_start_batch)
    mcp.tool("ozon_v2_workbench_allowed_actions")(ozon_v2_workbench_allowed_actions)
    mcp.tool("ozon_v2_workbench_dispatch")(ozon_v2_workbench_dispatch)

    mcp.resource("ozon-v2://runs/{run_id}/status")(get_run_status_resource)
    mcp.resource("ozon-v2://runs/{run_id}/artifacts")(get_artifacts_resource)

    return mcp


app = create_app()


if __name__ == "__main__":
    app.run()
