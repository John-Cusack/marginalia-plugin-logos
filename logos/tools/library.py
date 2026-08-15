"""logos.library — Search user's Logos library metadata."""

from __future__ import annotations

import json
from urllib.parse import urlencode

from research_engine.plugins.sdk import tool

from logos.http.client import logos_client


@tool(
    id="logos.library",
    description="Search the user's Logos library. Returns matching resources with metadata.",
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query for library resources.",
            },
            "type": {
                "type": "string",
                "description": 'Filter by resource type (e.g., "commentary", "dictionary", "bible").',
            },
            "limit": {
                "type": "integer",
                "description": "Max results to return. Defaults to 20.",
                "default": 20,
            },
            "include_unlicensed": {
                "type": "boolean",
                "description": "Include resources the user doesn't own. Defaults to false.",
                "default": False,
            },
        },
        "required": ["query"],
    },
)
async def handler(
    query: str,
    type: str | None = None,
    limit: int = 20,
    include_unlicensed: bool = False,
    **kwargs,
) -> str:
    params: dict[str, str] = {"query": query, "limit": str(limit)}
    if type:
        params["type"] = type
    if include_unlicensed:
        params["includeUnlicensedResources"] = "true"
    data = await logos_client.get(f"/api/app/library?{urlencode(params)}")
    return json.dumps(data, indent=2)
