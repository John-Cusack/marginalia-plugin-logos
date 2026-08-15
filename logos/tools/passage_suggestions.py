"""logos.passage_suggestions — Get passage suggestions."""

from __future__ import annotations

import json
from urllib.parse import quote

from research_engine.plugins.sdk import tool

from logos.http.client import logos_client


@tool(
    id="logos.passage_suggestions",
    description="Get passage suggestions as you type. Returns matching Bible references for partial input.",
    input_schema={
        "type": "object",
        "properties": {
            "input": {
                "type": "string",
                "description": "Partial passage text to get suggestions for.",
            },
        },
        "required": ["input"],
    },
)
async def handler(input: str, **kwargs) -> str:
    data = await logos_client.get(f"/api/app/suggestions/passage?input={quote(input)}")
    return json.dumps(data, indent=2)
