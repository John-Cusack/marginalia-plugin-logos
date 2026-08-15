"""logos.workflows — List available workflow templates."""

from __future__ import annotations

import json

from research_engine.plugins.sdk import tool

from logos.http.client import logos_client


@tool(
    id="logos.workflows",
    description="List available Logos workflow templates. Returns workflow definitions that can guide research processes.",
    input_schema={"type": "object", "properties": {}},
)
async def handler(**kwargs) -> str:
    data = await logos_client.get("/api/app/guides-menu/workflowTemplates")
    return json.dumps(data, indent=2)
