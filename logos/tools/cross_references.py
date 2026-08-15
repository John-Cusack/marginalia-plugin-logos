"""logos.cross_references — Get cross-references for a passage."""

from __future__ import annotations

import json

from research_engine.plugins.sdk import tool

from logos.http.client import logos_client


@tool(
    id="logos.cross_references",
    description="Get cross-references for a Bible passage. Returns related passages that illuminate the given text.",
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
        "/api/app/insights/crossReferences",
        {"contents": {"reference": reference, "language": "en-US"}},
    )
    return json.dumps(data, indent=2)
