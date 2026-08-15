"""logos.exegetical_guide — Original language analysis."""

from __future__ import annotations

import json
from urllib.parse import quote

from research_engine.plugins.sdk import tool

from logos.http.client import logos_client


@tool(
    id="logos.exegetical_guide",
    description="Get the Exegetical Guide for a Bible reference. Returns original language analysis including lemmas, morphology, syntax, word studies, and textual variants.",
    input_schema={
        "type": "object",
        "properties": {
            "reference": {
                "type": "string",
                "description": "Bible reference in Logos format or natural language.",
            },
        },
        "required": ["reference"],
    },
)
async def handler(reference: str, **kwargs) -> str:
    data = await logos_client.get(
        f"/api/app/guides/exegetical?reference={quote(reference)}"
    )
    return json.dumps(data, indent=2)
