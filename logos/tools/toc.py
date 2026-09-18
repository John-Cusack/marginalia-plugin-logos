"""logos.toc — Get table of contents for a Logos resource."""

from __future__ import annotations

import json
from urllib.parse import quote

from research_engine_sdk import tool

from logos.lib.context import binds_context
from logos.http.client import logos_client


@tool(
    id="logos.toc",
    description="Get the table of contents for a Logos resource/book. Returns the hierarchical structure with section IDs.",
    input_schema={
        "type": "object",
        "properties": {
            "resource_id": {
                "type": "string",
                "description": "The Logos resource ID.",
            },
        },
        "required": ["resource_id"],
    },
)
@binds_context
async def handler(resource_id: str, **kwargs) -> str:
    data = await logos_client.get(
        f"/api/app/books/{quote(resource_id)}/tableofcontents"
    )
    return json.dumps(data, indent=2)
