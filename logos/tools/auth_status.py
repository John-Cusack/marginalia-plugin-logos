"""logos.auth_status — Check authentication status."""

from __future__ import annotations

import json

from research_engine.plugins.sdk import tool

from logos.auth.manager import verify_auth


@tool(
    id="logos.auth_status",
    description="Check Logos authentication status. Verifies cookies and returns user info from /api/app/me.",
    input_schema={"type": "object", "properties": {}},
)
async def handler(**kwargs) -> str:
    result = await verify_auth()
    return json.dumps(result, indent=2)
