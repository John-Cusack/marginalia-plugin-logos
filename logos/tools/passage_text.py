"""logos.passage_text — Get Bible passage text."""

from __future__ import annotations

import json

from research_engine.plugins.sdk import tool

from logos.http.client import logos_client


@tool(
    id="logos.passage_text",
    description="Get Bible passage text in one or more translations. Returns verse-by-verse text.",
    input_schema={
        "type": "object",
        "properties": {
            "reference": {
                "type": "string",
                "description": 'Bible reference in Logos format (e.g., "bible.62.3.16-bible.62.3.16") or natural language (e.g., "John 3:16").',
            },
            "versions": {
                "type": "array",
                "items": {"type": "string"},
                "description": 'Bible version abbreviations (e.g., ["ESV", "NASB", "LEB"]). Defaults to ["LEB"].',
            },
        },
        "required": ["reference"],
    },
)
async def handler(reference: str, versions: list[str] | None = None, **kwargs) -> str:
    versions = versions or ["LEB"]
    body = {"passages": [{"passageId": reference}], "resourceIds": versions}
    data = await logos_client.post(
        "/api/app/tools/text-comparison/comparisonV2/verses", body
    )
    return json.dumps(data, indent=2)
