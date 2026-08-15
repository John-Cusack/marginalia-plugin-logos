"""logos.credits — Check feature credit usage."""

from __future__ import annotations

import json

from research_engine.plugins.sdk import tool

from logos.http.client import logos_client


@tool(
    id="logos.credits",
    description="Check Logos feature credit usage. Shows remaining credits for AI features and other metered services.",
    input_schema={"type": "object", "properties": {}},
)
async def handler(**kwargs) -> str:
    data = await logos_client.post("/api/feature-credits/usage", {})
    return json.dumps(data, indent=2)
