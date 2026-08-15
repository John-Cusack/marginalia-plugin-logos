"""logos.parallel_passages — Get synoptic/thematic parallels."""

from __future__ import annotations

import json

from research_engine.plugins.sdk import tool

from logos.http.client import logos_client


@tool(
    id="logos.parallel_passages",
    description="Get parallel passages for a Bible reference. Returns synoptic and thematic parallels.",
    input_schema={
        "type": "object",
        "properties": {
            "reference": {
                "type": "string",
                "description": 'Bible reference (e.g., "bible+esv.66.13.3" or "bible.66.13.3").',
            },
        },
        "required": ["reference"],
    },
)
async def handler(reference: str, **kwargs) -> str:
    data = await logos_client.post(
        "/api/app/insights/parallelPassages",
        {"contents": {"reference": reference, "language": "en-US"}},
    )
    return json.dumps(data, indent=2)
