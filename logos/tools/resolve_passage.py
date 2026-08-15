"""logos.resolve_passage — Resolve natural language Bible reference."""

from __future__ import annotations

import json
from urllib.parse import quote

from research_engine.plugins.sdk import tool

from logos.http.client import logos_client


@tool(
    id="logos.resolve_passage",
    description="Resolve a natural language Bible reference to its canonical Logos reference format.",
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": 'Natural language Bible reference (e.g., "John 3:16", "Genesis 1:1-3").',
            },
        },
        "required": ["query"],
    },
)
async def handler(query: str, **kwargs) -> str:
    data = await logos_client.get(f"/api/app/userCommand?commandText={quote(query)}")
    return json.dumps(data, indent=2)
