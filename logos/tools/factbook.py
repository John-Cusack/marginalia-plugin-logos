"""logos.factbook — Factbook report for biblical topics."""

from __future__ import annotations

import json

from research_engine_sdk import tool

from logos.lib.context import binds_context
from logos.http.client import logos_client


@tool(
    id="logos.factbook",
    description="Get a Factbook report for a biblical person, place, thing, or event. Returns structured information from Logos reference works.",
    input_schema={
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": 'The topic to look up (e.g., "Jesus", "Jerusalem", "Passover").',
            },
        },
        "required": ["topic"],
    },
)
@binds_context
async def handler(topic: str, **kwargs) -> str:
    data = await logos_client.post(
        "/api/app/library-reports/generate", {"topic": topic}
    )
    return json.dumps(data, indent=2)
