"""logos.word_study — Hebrew/Greek word study guide."""

from __future__ import annotations

import json
from urllib.parse import quote

from research_engine.plugins.sdk import tool

from logos.http.client import logos_client


@tool(
    id="logos.word_study",
    description="Get a Word Study Guide for a Hebrew or Greek word. Returns semantic domains, glosses, occurrences, and related words.",
    input_schema={
        "type": "object",
        "properties": {
            "word": {
                "type": "string",
                "description": "Hebrew/Greek word or lemma reference.",
            },
        },
        "required": ["word"],
    },
)
async def handler(word: str, **kwargs) -> str:
    data = await logos_client.get(f"/api/app/guides/word?reference={quote(word)}")
    return json.dumps(data, indent=2)
