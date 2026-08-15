"""logos.record_authority — Record scholar authority score."""

from __future__ import annotations

from research_engine.plugins.sdk import tool

from logos.db.migrate import run_migrations
from logos.db.queries import upsert_authority


@tool(
    id="logos.record_authority",
    description="Record a scholar's authority score for a passage range in the knowledge graph.",
    input_schema={
        "type": "object",
        "properties": {
            "scholar_name": {
                "type": "string",
                "description": "Full name of the scholar.",
            },
            "passage_book": {
                "type": "string",
                "description": 'Bible book (e.g., "Romans").',
            },
            "passage_start": {
                "type": "string",
                "description": "Start of passage range.",
            },
            "passage_end": {
                "type": "string",
                "description": "End of passage range.",
            },
            "authority_score": {
                "type": "number",
                "description": "Authority score 0-1 (1 = highest authority).",
            },
            "score_reasons": {
                "type": "object",
                "description": "JSON object explaining score components.",
            },
            "work_title": {
                "type": "string",
                "description": "Title of the work.",
            },
            "series_name": {
                "type": "string",
                "description": "Commentary series name.",
            },
            "series_tier": {
                "type": "integer",
                "description": "Series tier (1-5).",
            },
        },
        "required": ["scholar_name", "passage_book", "passage_start", "passage_end", "authority_score", "score_reasons"],
    },
)
async def handler(
    scholar_name: str,
    passage_book: str,
    passage_start: str,
    passage_end: str,
    authority_score: float,
    score_reasons: dict,
    work_title: str | None = None,
    series_name: str | None = None,
    series_tier: int | None = None,
    **kwargs,
) -> str:
    await run_migrations()
    record_id = await upsert_authority({
        "scholar_name": scholar_name,
        "passage_book": passage_book,
        "passage_start": passage_start,
        "passage_end": passage_end,
        "authority_score": authority_score,
        "score_reasons": score_reasons,
        "work_title": work_title,
        "series_name": series_name,
        "series_tier": series_tier,
    })
    return f"Authority record created with ID: {record_id}"
