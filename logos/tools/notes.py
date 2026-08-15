"""logos.notes — Get user notes and highlights."""

from __future__ import annotations

import json

from research_engine.plugins.sdk import tool

from logos.http.client import logos_client


@tool(
    id="logos.notes",
    description="Get user notes and highlights for a Bible passage or resource. Returns annotations the user has made.",
    input_schema={
        "type": "object",
        "properties": {
            "reference": {
                "type": "string",
                "description": "Bible reference or resource reference to get notes for.",
            },
        },
        "required": ["reference"],
    },
)
async def handler(reference: str, **kwargs) -> str:
    data = await logos_client.post(
        "/api/sinaix/resources/markup/noteshighlights", {"reference": reference}
    )
    return json.dumps(data, indent=2)
