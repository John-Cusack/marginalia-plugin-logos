"""logos.gap_analysis — Analyze gaps between scholars and owned resources."""

from __future__ import annotations

import json

from research_engine_sdk import tool

from logos.lib.context import binds_context
from logos.db.queries import gap_analysis


@tool(
    id="logos.gap_analysis",
    description="Analyze gaps between authority-ranked scholars and owned Logos resources for a Bible book.",
    input_schema={
        "type": "object",
        "properties": {
            "passage_book": {
                "type": "string",
                "description": 'Bible book to analyze (e.g., "Romans", "Genesis").',
            },
        },
        "required": ["passage_book"],
    },
)
@binds_context
async def handler(passage_book: str, **kwargs) -> str:
    results = await gap_analysis(passage_book)
    owned = [r for r in results if r.get("logos_owned") is True]
    not_owned = [r for r in results if r.get("logos_owned") is not True]

    summary = {
        "book": passage_book,
        "total_scholars": len(results),
        "owned_resources": len(owned),
        "gaps": len(not_owned),
        "top_gaps": not_owned[:10],
        "owned": owned,
    }
    return json.dumps(summary, indent=2, default=str)
