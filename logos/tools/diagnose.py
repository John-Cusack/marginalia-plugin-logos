"""logos.diagnose MCP tool — thin wrapper over ``logos.auth.diagnose.run_diagnose``."""

from __future__ import annotations

from typing import Any

from research_engine_sdk import tool

from logos.lib.context import binds_context
from logos.auth.diagnose import run_diagnose


@tool(
    id="logos.diagnose",
    description=(
        "Comprehensive Logos auth diagnostic. Returns the state of every layer "
        "(cookie file, in-memory jar, cache freshness, live /api/app/me check) "
        "so you can localize a 'not authenticated' failure in one call."
    ),
    input_schema={"type": "object", "properties": {}},
)
@binds_context
async def handler(**kwargs: Any) -> dict[str, Any]:
    return await run_diagnose()
