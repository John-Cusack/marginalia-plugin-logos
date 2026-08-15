"""logos.commentary — Get commentary and study resources."""

from __future__ import annotations

import asyncio
import json

from research_engine.plugins.sdk import tool

from logos.http.client import logos_client
from logos.parsers.xml_to_markdown import xml_to_markdown

DEFAULT_RESOURCE_SETS = ["BibleCommentaries", "StudyBibles", "TextualCommentaries"]


def _build_best_resources_body(
    reference: str,
    resource_set: str,
    character_limit: int = 750,
) -> dict:
    return {
        "contents": {
            "resourceSet": resource_set,
            "excludedResourceIds": [],
            "position": {
                "reference": {
                    "value": reference,
                    "display": reference,
                },
            },
            "richTextSettings": {
                "removeMultiColumnTable": {
                    "text": "Open the book to view the table.",
                    "link": "",
                },
                "characterLimit": {
                    "full": character_limit,
                    "truncated": 125,
                },
            },
            "limit": 3,
            "language": "en-US",
        }
    }


def _parse_resource_response(data: dict, resource_set: str) -> str:
    sections: list[str] = [f"## {resource_set}"]

    value = data.get("value", data) if isinstance(data, dict) else data
    resources = value.get("resources") if isinstance(value, dict) else None
    if isinstance(resources, list):
        for resource in resources:
            if not isinstance(resource, dict):
                continue
            title = resource.get("title", "Untitled")
            sections.append(f"### {title}")

            rich_text = resource.get("filteredContentRichText", {})
            content = (
                rich_text.get("full")
                or resource.get("content")
                or resource.get("text")
            )
            if isinstance(content, str):
                sections.append(xml_to_markdown(content))
            elif content is not None:
                sections.append(json.dumps(content))
    else:
        content = (
            value.get("content") or value.get("text") or json.dumps(value)
            if isinstance(value, dict)
            else str(value)
        )
        if isinstance(content, str):
            sections.append(xml_to_markdown(content))
        else:
            sections.append(str(content))

    return "\n\n".join(sections)


@tool(
    id="logos.commentary",
    description="Get commentary and study resources for a Bible passage. Queries multiple resource sets in parallel and returns parsed Markdown content.",
    input_schema={
        "type": "object",
        "properties": {
            "reference": {
                "type": "string",
                "description": 'Bible reference (e.g., "bible+esv.66.13.3" or "bible.66.13.3").',
            },
            "resource_sets": {
                "type": "array",
                "items": {"type": "string"},
                "description": 'Resource sets to query. Options: "BibleCommentaries", "StudyBibles", "TextualCommentaries", "OriginalLanguageBibles", "AncientLanguageBibles".',
            },
            "character_limit": {
                "type": "integer",
                "description": "Max characters per resource. Defaults to 5000.",
                "default": 5000,
            },
        },
        "required": ["reference"],
    },
)
async def handler(
    reference: str,
    resource_sets: list[str] | None = None,
    character_limit: int = 5000,
    **kwargs,
) -> str:
    sets = resource_sets or list(DEFAULT_RESOURCE_SETS)

    async def _fetch_set(rs: str) -> str:
        try:
            body = _build_best_resources_body(reference, rs, character_limit)
            data = await logos_client.post(
                "/api/app/insights/bestResources", body
            )
            return _parse_resource_response(data, rs)
        except Exception as e:
            return f"## {rs}\n\n*Error: {e}*"

    results = await asyncio.gather(*[_fetch_set(rs) for rs in sets])
    return "\n\n---\n\n".join(results)
