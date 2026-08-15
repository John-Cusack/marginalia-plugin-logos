"""logos.passage_guide — Get the Passage Guide."""

from __future__ import annotations

import json
from urllib.parse import quote

from research_engine.plugins.sdk import tool

from logos.http.client import logos_client


@tool(
    id="logos.passage_guide",
    description="Get the Passage Guide for a Bible reference. Returns a comprehensive overview including commentaries, cross-references, literary typing, and more.",
    input_schema={
        "type": "object",
        "properties": {
            "reference": {
                "type": "string",
                "description": 'Bible reference in Logos format (e.g., "bible.62.3.16") or natural language.',
            },
        },
        "required": ["reference"],
    },
)
async def handler(reference: str, **kwargs) -> str:
    data = await logos_client.get(
        f"/api/app/guides/passage?reference={quote(reference)}"
    )
    return json.dumps(data, indent=2)
