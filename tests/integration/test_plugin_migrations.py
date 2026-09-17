"""Plugin migrations against a real, disposable Postgres.

Set ``LOGOS_TEST_DB_URL`` to a server where the user may ``CREATE DATABASE``;
every test runs in a database of its own and drops it afterwards. Never point it
at a corpus you care about — nothing here touches an existing database, but the
URL is a superuser's in any sensible setup.

    docker run -d --rm --name logos-pg -p 127.0.0.1:55441:5432 \\
        -e POSTGRES_PASSWORD=pw postgres:16
    LOGOS_TEST_DB_URL=postgresql://postgres:pw@127.0.0.1:55441/postgres \\
        uv run pytest tests/integration/test_plugin_migrations.py -q
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from research_engine_sdk import PluginContext

from logos.db.migrate import (
    LEDGER_TABLE,
    MigrationDriftError,
    apply_upgrade,
    discover_migrations,
    read_status,
    status,
    upgrade,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = [pytest.mark.integration]

ADMIN_URL = os.environ.get("LOGOS_TEST_DB_URL")

LOGOS_TABLES = [
    "logos_api_calls",
    "logos_authority",
    "logos_ingest_article_texts",
    "logos_ingest_chunks",
    "logos_ingest_progress",
    "logos_resources",
    "logos_scholars",
]

#: The DDL every tool ran lazily before 0.2.0, verbatim from
#: ``logos/db/migrate.py`` at the last commit that had it. A database created
#: by it is what every existing installation has.
LEGACY_SCHEMA_SQL = """
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


def _with_database(url: str, name: str) -> str:
    return urlunsplit(urlsplit(url)._replace(path=f"/{name}"))


@pytest.fixture
async def database() -> AsyncIterator[str]:
    """A freshly created, empty database; dropped after the test."""
    if not ADMIN_URL:
        pytest.skip("set LOGOS_TEST_DB_URL to run plugin migration tests")
    try:
        admin = await asyncpg.connect(ADMIN_URL, timeout=5)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"LOGOS_TEST_DB_URL is unreachable: {exc}")
    name = f"logos_migrate_{uuid.uuid4().hex[:12]}"
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        yield _with_database(ADMIN_URL, name)
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def _tables(dsn: str) -> set[str]:
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE tablename LIKE 'logos\\_%'"
        )
        return {r["tablename"] for r in rows}
    finally:
        await conn.close()


async def _ledger(dsn: str) -> list[dict]:
    conn = await asyncpg.connect(dsn)
    try:
        return [dict(r) for r in await conn.fetch(f"SELECT * FROM {LEDGER_TABLE} ORDER BY revision")]
    finally:
        await conn.close()


async def _snapshot(dsn: str) -> dict:
    """Row counts for every plugin table, plus the checkpoints themselves."""
    conn = await asyncpg.connect(dsn)
    try:
        counts = {t: await conn.fetchval(f"SELECT count(*) FROM {t}") for t in LOGOS_TABLES}
        progress = [
            dict(r) for r in await conn.fetch(
                "SELECT * FROM logos_ingest_progress ORDER BY resource_id"
            )
        ]
        chunks = [
            dict(r) for r in await conn.fetch("SELECT * FROM logos_ingest_chunks ORDER BY id")
        ]
        return {"counts": counts, "progress": progress, "chunks": chunks}
    finally:
        await conn.close()


class TestEmptyDatabase:
    async def test_status_before_upgrade_is_revision_zero(self, database):
        state = await read_status(database)
        assert (state.current_revision, state.target_revision) == (0, 1)
        assert state.pending == [1] and state.applied == [] and not state.up_to_date
        assert await _tables(database) == set(), "status must not write"

    async def test_upgrade_creates_every_table_and_records_the_revision(self, database):
        state = await apply_upgrade(database)

        assert state.up_to_date and state.current_revision == 1
        assert await _tables(database) == {*LOGOS_TABLES, LEDGER_TABLE}
        (row,) = await _ledger(database)
        (migration,) = discover_migrations()
        assert (row["revision"], row["name"], row["checksum"]) == (
            1, "initial", migration.checksum
        )
        assert row["applied_at"] is not None

    async def test_indexes_are_created(self, database):
        await apply_upgrade(database)
        conn = await asyncpg.connect(database)
        try:
            names = {
                r["indexname"] for r in await conn.fetch(
                    "SELECT indexname FROM pg_indexes WHERE tablename LIKE 'logos\\_%'"
                )
            }
        finally:
            await conn.close()
        assert {
            "idx_logos_authority_book", "idx_logos_authority_scholar",
            "idx_logos_authority_score", "idx_logos_chunks_resource_status",
            "idx_logos_chunks_batch",
        } <= names

    async def test_a_second_upgrade_is_a_no_op(self, database):
        await apply_upgrade(database)
        before = await _ledger(database)

        state = await apply_upgrade(database)

        assert state.up_to_date
        assert await _ledger(database) == before

    async def test_the_manifest_entries_as_core_calls_them(self, database, tmp_path: Path):
        """Keywords only, core's SQLAlchemy-style URL, a mapping back."""
        context = PluginContext(
            plugin_id="logos",
            data_dir=tmp_path,
            distribution_name="marginalia-ai-plugin-logos",
            distribution_version="0.2.0",
        )
        url = database.replace("postgresql://", "postgresql+asyncpg://", 1)

        before = await status(context=context, database_url=url)
        assert (before["current_revision"], before["status"]) == (0, "pending")
        after = await upgrade(context=context, database_url=url)
        assert (after["current_revision"], after["status"]) == (1, "ok")
        assert (await status(context=context, database_url=url))["status"] == "ok"
        json.dumps(after)


class TestDrift:
    async def test_a_tampered_checksum_is_reported_and_blocks_upgrade(self, database):
        await apply_upgrade(database)
        conn = await asyncpg.connect(database)
        try:
            await conn.execute(f"UPDATE {LEDGER_TABLE} SET checksum = 'tampered'")
        finally:
            await conn.close()
        before = await _ledger(database)

        state = await read_status(database)
        assert state.drift == [1] and not state.up_to_date
        assert state.current_revision == 0, "core must see a drifted database as behind"

        with pytest.raises(MigrationDriftError, match="nothing was applied"):
            await apply_upgrade(database)
        assert await _ledger(database) == before

    async def test_an_applied_revision_this_package_does_not_ship_blocks_upgrade(self, database):
        await apply_upgrade(database)
        conn = await asyncpg.connect(database)
        try:
            await conn.execute(
                f"INSERT INTO {LEDGER_TABLE} (revision, name, checksum) "
                f"VALUES (2, 'from_a_newer_release', 'x')"
            )
        finally:
            await conn.close()

        assert (await read_status(database)).drift == [2]
        with pytest.raises(MigrationDriftError):
            await apply_upgrade(database)

    async def test_drift_refusal_applies_no_pending_revision(self, database):
        """A drifted revision 1 must not let a pending revision 2 through."""
        (initial,) = discover_migrations()
        await apply_upgrade(database, migrations=[initial])
        tampered = replace(initial, checksum="0" * 64)
        second = replace(
            initial, revision=2, name="second",
            sql="CREATE TABLE logos_should_not_exist (id INT);", checksum="1" * 64,
        )

        with pytest.raises(MigrationDriftError):
            await apply_upgrade(database, migrations=[tampered, second])
        assert "logos_should_not_exist" not in await _tables(database)


class TestConcurrency:
    async def test_two_concurrent_upgrades_apply_the_migration_once(self, database):
        """Slow the migration so both callers read an empty ledger unless the lock
        serialises them. Without it the second insert of revision 1 fails."""
        (initial,) = discover_migrations()
        slow = replace(initial, sql="SELECT pg_sleep(0.5);\n" + initial.sql)

        results = await asyncio.gather(
            apply_upgrade(database, migrations=[slow]),
            apply_upgrade(database, migrations=[slow]),
        )

        assert all(r.up_to_date for r in results)
        assert len(await _ledger(database)) == 1


class TestLegacyDatabase:
    """An installation from before 0.2.0: tables made by the lazy DDL, full of data."""

    async def _populate(self, dsn: str) -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(LEGACY_SCHEMA_SQL)
            await conn.execute(
                "INSERT INTO logos_scholars (name, birth_year, primary_field, subfields) "
                "VALUES ('D. A. Carson', 1946, 'NT', ARRAY['Johannine'])"
            )
            await conn.execute(
                "INSERT INTO logos_authority (scholar_name, passage_book, passage_start, "
                "passage_end, authority_score, score_reasons) "
                "VALUES ('D. A. Carson', 'John', 'John 1:1', 'John 21:25', 0.9, $1)",
                json.dumps({"series": "PNTC"}),
            )
            await conn.execute(
                "INSERT INTO logos_resources (resource_id, title, ingested, chunk_count) "
                "VALUES ('LLS:PNTCJOHN', 'The Gospel according to John', true, 1200)"
            )
            await conn.execute(
                "INSERT INTO logos_api_calls (endpoint, status_code, duration_ms) "
                "VALUES ('/api/app/resources', 200, 41)"
            )
            for resource_id, last, index, total, done in [
                ("LLS:46.30.25", "R.A.2", 2, 188724, False),
                ("LLS:PNTCJOHN", "END", 900, 900, True),
            ]:
                await conn.execute(
                    "INSERT INTO logos_ingest_progress (resource_id, resource_title, "
                    "abbreviated_title, last_article_id, last_article_index, "
                    "total_articles, walk_complete, authors) "
                    "VALUES ($1, $2, 'LSJ', $3, $4, $5, $6, ARRAY['Liddell','Scott'])",
                    resource_id, f"Title of {resource_id}", last, index, total, done,
                )
            for article_id, status_ in [("R.A.1", "stored"), ("R.A.2", "pending"), ("R.A.3", "failed")]:
                await conn.execute(
                    "INSERT INTO logos_ingest_chunks (resource_id, article_id, batch_key, "
                    "draft_json, status, core_document_id, error) "
                    "VALUES ('LLS:46.30.25', $1, 'b0000', $2, $3, $4, $5)",
                    article_id,
                    json.dumps({"position": 0, "char_start": 0, "char_end": 5, "text": "ἀλλήλ",
                                "chunker": "verse_boundary", "chunker_version": "5.0"}),
                    status_,
                    uuid.uuid4() if status_ == "stored" else None,
                    "embedding unavailable" if status_ == "failed" else None,
                )
                await conn.execute(
                    "INSERT INTO logos_ingest_article_texts (resource_id, article_id, text) "
                    "VALUES ('LLS:46.30.25', $1, $2)",
                    article_id, f"text of {article_id}",
                )
        finally:
            await conn.close()

    async def test_upgrade_adopts_it_without_changing_a_row(self, database):
        await self._populate(database)
        before = await _snapshot(database)
        assert all(before["counts"].values()), "every table should hold data first"

        pre = await read_status(database)
        assert pre.current_revision == 0 and pre.pending == [1]

        state = await apply_upgrade(database)

        assert state.up_to_date and state.current_revision == 1
        after = await _snapshot(database)
        assert after == before
        assert await _tables(database) == {*LOGOS_TABLES, LEDGER_TABLE}
