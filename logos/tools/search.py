"""logos.search — Full-text search across Logos books."""

from __future__ import annotations

import json

from research_engine.plugins.sdk import tool

from logos.http.client import logos_client


@tool(
    id="logos.search",
    description="Search across Logos books and resources. Returns matching passages with context.",
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query text.",
            },
            "scope": {
                "type": "string",
                "description": "Optional scope to limit search (e.g., a resource ID).",
            },
            "limit": {
                "type": "integer",
                "description": "Max results to return. Defaults to 20.",
                "default": 20,
            },
        },
        "required": ["query"],
    },
)
async def handler(query: str, scope: str | None = None, limit: int = 20, **kwargs) -> str:
    body: dict = {"query": query, "limit": limit}
    if scope:
        body["scope"] = scope
    data = await logos_client.post("/api/app/search/v2/books", body)
    return json.dumps(data, indent=2)
