"""Query functions for logos_ prefixed tables."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from logos.db.pool import get_pool


# ---------- Scholars ----------


async def insert_scholar(data: dict[str, Any]) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO logos_scholars (name, birth_year, death_year, primary_field, subfields, institutions, tradition, confidence)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (name) DO UPDATE SET
                birth_year = COALESCE(EXCLUDED.birth_year, logos_scholars.birth_year),
                death_year = COALESCE(EXCLUDED.death_year, logos_scholars.death_year),
                primary_field = COALESCE(EXCLUDED.primary_field, logos_scholars.primary_field),
                subfields = COALESCE(EXCLUDED.subfields, logos_scholars.subfields),
                institutions = COALESCE(EXCLUDED.institutions, logos_scholars.institutions),
                tradition = COALESCE(EXCLUDED.tradition, logos_scholars.tradition),
                updated_at = NOW()
            RETURNING id""",
            data["name"],
            data.get("birth_year"),
            data.get("death_year"),
            data.get("primary_field"),
            data.get("subfields"),
            data.get("institutions"),
            data.get("tradition"),
            data.get("confidence", 0.5),
        )
        return row["id"] if row else 0


async def search_scholars(filters: dict[str, Any]) -> list[dict]:
    pool = await get_pool()
    conditions: list[str] = []
    params: list[Any] = []
    idx = 1

    if filters.get("name"):
        conditions.append(f"s.name ILIKE ${idx}")
        params.append(f"%{filters['name']}%")
        idx += 1
    if filters.get("field"):
        conditions.append(f"(s.primary_field ILIKE ${idx} OR ${idx + 1} = ANY(s.subfields))")
        params.extend([f"%{filters['field']}%", f"%{filters['field']}%"])
        idx += 2

    book_filter = ""
    if filters.get("passage_book"):
        book_filter = f"AND la.passage_book = ${idx}"
        params.append(filters["passage_book"])
        idx += 1

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    sql = f"""
        SELECT s.*,
            COALESCE(
                (SELECT json_agg(json_build_object(
                    'passage_book', la.passage_book,
                    'authority_score', la.authority_score,
                    'work_title', la.work_title
                ))
                FROM logos_authority la
                WHERE la.scholar_name = s.name {book_filter}
                ), '[]'
            ) as authorities
        FROM logos_scholars s
        {where}
        ORDER BY s.confidence DESC
        LIMIT 50
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]


# ---------- Authority ----------


async def upsert_authority(data: dict[str, Any]) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO logos_authority
                (scholar_name, passage_book, passage_start, passage_end,
                 authority_score, score_reasons, work_title, series_name, series_tier)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT DO NOTHING
            RETURNING id""",
            data["scholar_name"],
            data["passage_book"],
            data["passage_start"],
            data["passage_end"],
            data["authority_score"],
            json.dumps(data["score_reasons"]),
            data.get("work_title"),
            data.get("series_name"),
            data.get("series_tier"),
        )
        return row["id"] if row else 0


# ---------- Gap Analysis ----------


async def gap_analysis(passage_book: str) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT
                la.scholar_name, la.authority_score, la.work_title,
                la.series_name, la.series_tier,
                lr.resource_id as logos_resource_id,
                CASE WHEN lr.id IS NOT NULL THEN true ELSE false END as logos_owned
            FROM logos_authority la
            LEFT JOIN logos_resources lr ON lr.title = la.work_title
            WHERE la.passage_book = $1
            ORDER BY la.authority_score DESC""",
            passage_book,
        )
        return [dict(r) for r in rows]


# ---------- Ingest Progress ----------


async def get_ingest_progress(resource_id: str) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM logos_ingest_progress WHERE resource_id = $1",
            resource_id,
        )
        return dict(row) if row else None


async def upsert_ingest_progress(
    resource_id: str,
    title: str,
    abbreviated: str,
    last_article_id: str,
    index: int,
    total: int,
    walk_complete: bool,
    authors: list[str],
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO logos_ingest_progress
                (resource_id, resource_title, abbreviated_title,
                 last_article_id, last_article_index, total_articles,
                 walk_complete, authors, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
            ON CONFLICT (resource_id) DO UPDATE SET
                resource_title = EXCLUDED.resource_title,
                abbreviated_title = EXCLUDED.abbreviated_title,
                last_article_id = EXCLUDED.last_article_id,
                last_article_index = EXCLUDED.last_article_index,
                total_articles = EXCLUDED.total_articles,
                walk_complete = EXCLUDED.walk_complete,
                authors = EXCLUDED.authors,
                updated_at = NOW()""",
            resource_id,
            title,
            abbreviated,
            last_article_id,
            index,
            total,
            walk_complete,
            authors,
        )


# ---------- Ingest Chunks ----------


async def insert_chunks(
    resource_id: str,
    article_id: str,
    batch_key: str,
    drafts: list[dict],
) -> int:
    """Insert serialized PassageDraft dicts as pending chunk rows. Returns count inserted."""
    if not drafts:
        return 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(
            """INSERT INTO logos_ingest_chunks
                (resource_id, article_id, batch_key, draft_json)
            VALUES ($1, $2, $3, $4::jsonb)""",
            [(resource_id, article_id, batch_key, json.dumps(d)) for d in drafts],
        )
    return len(drafts)


async def save_article_text(resource_id: str, article_id: str, text: str) -> None:
    """Keep an article's source text so batch offsets can be rebased onto it.

    Idempotent: a resumed run re-processing an article replaces the text rather
    than colliding on the primary key.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO logos_ingest_article_texts (resource_id, article_id, text)
            VALUES ($1, $2, $3)
            ON CONFLICT (resource_id, article_id) DO UPDATE SET text = EXCLUDED.text""",
            resource_id,
            article_id,
            text,
        )


async def get_article_texts(resource_id: str, article_ids: list[str]) -> dict[str, str]:
    """Article texts for the given ids. Missing ids are simply absent."""
    if not article_ids:
        return {}
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT article_id, text FROM logos_ingest_article_texts
            WHERE resource_id = $1 AND article_id = ANY($2::text[])""",
            resource_id,
            article_ids,
        )
        return {r["article_id"]: r["text"] for r in rows}


async def get_pending_batch_keys(resource_id: str) -> list[str]:
    """Return distinct batch keys that still have pending chunks, ordered."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT DISTINCT batch_key FROM logos_ingest_chunks
            WHERE resource_id = $1 AND status = 'pending'
            ORDER BY batch_key""",
            resource_id,
        )
        return [r["batch_key"] for r in rows]


async def get_chunks_for_batch(resource_id: str, batch_key: str) -> list[dict]:
    """Return all chunk rows for a given batch key."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, article_id, draft_json FROM logos_ingest_chunks
            WHERE resource_id = $1 AND batch_key = $2 AND status = 'pending'
            ORDER BY id""",
            resource_id,
            batch_key,
        )
        return [
            {"id": r["id"], "article_id": r["article_id"], "draft_json": r["draft_json"]}
            for r in rows
        ]


async def mark_chunks_stored(
    resource_id: str, batch_key: str, document_id: UUID,
) -> int:
    """Mark all pending chunks in a batch as stored. Returns count updated."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """UPDATE logos_ingest_chunks
            SET status = 'stored', core_document_id = $3, stored_at = NOW()
            WHERE resource_id = $1 AND batch_key = $2 AND status = 'pending'""",
            resource_id,
            batch_key,
            document_id,
        )
        return int(result.split()[-1])


async def mark_chunks_failed(resource_id: str, batch_key: str, error: str) -> int:
    """Mark all pending chunks in a batch as failed. Returns count updated."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """UPDATE logos_ingest_chunks
            SET status = 'failed', error = $3
            WHERE resource_id = $1 AND batch_key = $2 AND status = 'pending'""",
            resource_id,
            batch_key,
            error,
        )
        return int(result.split()[-1])


async def reset_failed_chunks(resource_id: str) -> int:
    """Reset all failed chunks back to pending for retry. Returns count updated."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """UPDATE logos_ingest_chunks
            SET status = 'pending', error = NULL
            WHERE resource_id = $1 AND status = 'failed'""",
            resource_id,
        )
        return int(result.split()[-1])


async def get_max_batch_summary(resource_id: str) -> dict | None:
    """Return summary of the highest-keyed batch for a resource, or None.

    The walker's canonical resume resolver uses this to decide where to
    continue writing chunks. The returned dict has:
      - batch_key: str   (e.g. "b0007", or "b0007a" after halving)
      - count: int       (total chunk rows in that batch_key, any status)
      - any_stored: bool (whether any chunk in that batch is status='stored')
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT batch_key,
                      COUNT(*) AS count,
                      BOOL_OR(status = 'stored') AS any_stored
               FROM logos_ingest_chunks
               WHERE resource_id = $1
               GROUP BY batch_key
               ORDER BY batch_key DESC
               LIMIT 1""",
            resource_id,
        )
    if not row:
        return None
    return {
        "batch_key": row["batch_key"],
        "count": int(row["count"]),
        "any_stored": bool(row["any_stored"]),
    }


async def reassign_chunk_batch_keys(chunk_ids: list[int], new_batch_key: str) -> int:
    """Move specific chunk rows to a new batch key. Returns count updated."""
    if not chunk_ids:
        return 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """UPDATE logos_ingest_chunks
            SET batch_key = $2
            WHERE id = ANY($1::int[]) AND status = 'pending'""",
            chunk_ids,
            new_batch_key,
        )
        return int(result.split()[-1])
