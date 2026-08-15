"""logos.search_scholars — Search scholar authority records."""

from __future__ import annotations

import json

from research_engine.plugins.sdk import tool

from logos.db.migrate import run_migrations
from logos.db.queries import search_scholars


@tool(
    id="logos.search_scholars",
    description="Search the knowledge graph for biblical scholars by name, field, or passage expertise.",
    input_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Scholar name to search for (partial match).",
            },
            "field": {
                "type": "string",
                "description": 'Academic field to filter by (e.g., "New Testament", "Pauline studies").',
            },
            "passage_book": {
                "type": "string",
                "description": 'Bible book to find experts for (e.g., "John", "Romans").',
            },
        },
    },
)
async def handler(
    name: str | None = None,
    field: str | None = None,
    passage_book: str | None = None,
    **kwargs,
) -> str:
    await run_migrations()
    results = await search_scholars(
        {"name": name, "field": field, "passage_book": passage_book}
    )
    return json.dumps(results, indent=2, default=str)
