"""logos.ai_synopsis — AI synopsis of search results."""

from __future__ import annotations

import json

from research_engine.plugins.sdk import tool

from logos.http.client import logos_client


@tool(
    id="logos.ai_synopsis",
    description="Get an AI-generated synopsis of search results. Provides a summary based on search matches across your library.",
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query to generate a synopsis for.",
            },
        },
        "required": ["query"],
    },
)
async def handler(query: str, **kwargs) -> str:
    data = await logos_client.post(
        "/api/app/search/v2/getresultssynopsisv2", {"query": query}
    )
    return json.dumps(data, indent=2)
