"""logos.ingest_status — Check ingestion status for Logos books."""

from __future__ import annotations

from research_engine.plugins.sdk import tool

from logos.db.migrate import run_migrations
from logos.db.queries import get_ingest_progress


@tool(
    id="logos.ingest_status",
    description="Check ingestion status for one or all Logos books. "
    "Shows walk progress, chunk counts by status (pending/stored/failed), "
    "and whether the book is fully ingested into the corpus.",
    input_schema={
        "type": "object",
        "properties": {
            "resource_id": {
                "type": "string",
                "description": "Optional resource ID. If omitted, shows all tracked books.",
            },
        },
    },
)
async def handler(resource_id: str = "", **kwargs) -> dict:
    await run_migrations()

    from logos.db.pool import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        if resource_id:
            progress = await get_ingest_progress(resource_id)
            if not progress:
                return {"error": f"No ingestion record for {resource_id}"}
            counts = await conn.fetchrow(
                """SELECT
                    COUNT(*) FILTER (WHERE status = 'pending') AS pending,
                    COUNT(*) FILTER (WHERE status = 'stored') AS stored,
                    COUNT(*) FILTER (WHERE status = 'failed') AS failed
                FROM logos_ingest_chunks WHERE resource_id = $1""",
                resource_id,
            )
            return {
                "resource_id": resource_id,
                "title": progress["resource_title"],
                "walk_complete": progress["walk_complete"],
                "total_articles": progress["total_articles"],
                "chunks": {
                    "pending": counts["pending"],
                    "stored": counts["stored"],
                    "failed": counts["failed"],
                    "total": counts["pending"] + counts["stored"] + counts["failed"],
                },
                "fully_ingested": (
                    progress["walk_complete"]
                    and counts["pending"] == 0
                    and counts["failed"] == 0
                    and counts["stored"] > 0
                ),
            }
        else:
            rows = await conn.fetch(
                """SELECT
                    p.resource_id, p.resource_title, p.walk_complete, p.total_articles,
                    COUNT(*) FILTER (WHERE c.status = 'pending') AS pending,
                    COUNT(*) FILTER (WHERE c.status = 'stored') AS stored,
                    COUNT(*) FILTER (WHERE c.status = 'failed') AS failed
                FROM logos_ingest_progress p
                LEFT JOIN logos_ingest_chunks c ON c.resource_id = p.resource_id
                GROUP BY p.resource_id, p.resource_title, p.walk_complete, p.total_articles
                ORDER BY p.resource_title"""
            )
            books = []
            for r in rows:
                books.append({
                    "resource_id": r["resource_id"],
                    "title": r["resource_title"],
                    "walk_complete": r["walk_complete"],
                    "articles": r["total_articles"],
                    "stored": r["stored"],
                    "pending": r["pending"],
                    "failed": r["failed"],
                    "fully_ingested": (
                        r["walk_complete"]
                        and r["pending"] == 0
                        and r["failed"] == 0
                        and r["stored"] > 0
                    ),
                })
            return {"books": books}
