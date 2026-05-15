"""logos.diagnose MCP tool — thin wrapper over ``logos.auth.diagnose.run_diagnose``."""

from __future__ import annotations

from typing import Any

from research_engine.plugins.sdk import tool

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
async def handler(**kwargs: Any) -> dict[str, Any]:
    return await run_diagnose()
