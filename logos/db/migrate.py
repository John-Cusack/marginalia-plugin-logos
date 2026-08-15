"""Idempotent schema migration for logos_ prefixed tables."""

from __future__ import annotations

from logos.db.pool import get_pool
from logos.lib.logger import log

_SCHEMA_SQL = """
-- Scholar metadata
CREATE TABLE IF NOT EXISTS logos_scholars (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    birth_year INTEGER,
    death_year INTEGER,
    primary_field TEXT,
    subfields TEXT[],
    institutions TEXT[],
    tradition TEXT,
    confidence REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Authority scores per passage range
CREATE TABLE IF NOT EXISTS logos_authority (
    id SERIAL PRIMARY KEY,
    scholar_name TEXT NOT NULL,
    passage_book TEXT NOT NULL,
    passage_start TEXT NOT NULL,
    passage_end TEXT NOT NULL,
    authority_score REAL NOT NULL DEFAULT 0 CHECK (authority_score BETWEEN 0 AND 1),
    score_reasons JSONB NOT NULL DEFAULT '{}',
    work_title TEXT,
    series_name TEXT,
    series_tier INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_logos_authority_book ON logos_authority(passage_book);
CREATE INDEX IF NOT EXISTS idx_logos_authority_scholar ON logos_authority(scholar_name);
CREATE INDEX IF NOT EXISTS idx_logos_authority_score ON logos_authority(authority_score DESC);

-- Tracked Logos resources
CREATE TABLE IF NOT EXISTS logos_resources (
    id SERIAL PRIMARY KEY,
    resource_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    author TEXT,
    resource_type TEXT,
    ingested BOOLEAN NOT NULL DEFAULT FALSE,
    ingested_at TIMESTAMPTZ,
    chunk_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- API audit log
CREATE TABLE IF NOT EXISTS logos_api_calls (
    id SERIAL PRIMARY KEY,
    endpoint TEXT NOT NULL,
    method TEXT NOT NULL DEFAULT 'GET',
    status_code INTEGER,
    duration_ms INTEGER,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Ingest walk progress (checkpoint for resumable article walks)
CREATE TABLE IF NOT EXISTS logos_ingest_progress (
    resource_id TEXT PRIMARY KEY,
    resource_title TEXT NOT NULL,
    abbreviated_title TEXT NOT NULL DEFAULT '',
    last_article_id TEXT NOT NULL,
    last_article_index INTEGER NOT NULL DEFAULT 0,
    total_articles INTEGER NOT NULL DEFAULT 0,
    walk_complete BOOLEAN NOT NULL DEFAULT FALSE,
    authors TEXT[] NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Ingest chunk staging (holds PassageDrafts between walk and storage)
CREATE TABLE IF NOT EXISTS logos_ingest_chunks (
    id SERIAL PRIMARY KEY,
    resource_id TEXT NOT NULL,
    article_id TEXT NOT NULL,
    batch_key TEXT NOT NULL,
    draft_json JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    core_document_id UUID,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    stored_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_logos_chunks_resource_status
    ON logos_ingest_chunks(resource_id, status);
CREATE INDEX IF NOT EXISTS idx_logos_chunks_batch
    ON logos_ingest_chunks(resource_id, batch_key);

-- Article text kept alongside the staged chunks.
--
-- A stored document is a *batch* of chunks drawn from many articles, but the
-- chunker's offsets are relative to a single article. To make them address the
-- document, the batch's articles are concatenated into the document's canonical
-- text and each chunk's offsets are shifted by its article's position in that
-- concatenation. That needs the article text at batch time, which is here.
CREATE TABLE IF NOT EXISTS logos_ingest_article_texts (
    resource_id TEXT NOT NULL,
    article_id TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (resource_id, article_id)
);
"""

_migrated = False


async def run_migrations() -> None:
    global _migrated
    if _migrated:
        return

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_SCHEMA_SQL)

    _migrated = True
    log("Logos database migrations applied")
