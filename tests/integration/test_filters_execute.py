"""`ScriptureRefRangeFilter` must run, not merely compile.

The unit tests for this filter asserted on the compiled SQL string —
`assert "scripture_refs" in compiled` — which is true of a statement Postgres
refuses to plan. And it did refuse: `core.passages.metadata` is a `json` column,
every `jsonb_*` function takes `jsonb`, and so
`jsonb_array_elements_text(p.metadata->'scripture_refs')` raised

    function jsonb_array_elements_text(json) does not exist

for every argument, on every corpus, since the filter was written. A string
assertion cannot tell the difference between SQL that works and SQL that cannot
run at all, so these execute it instead.

They build their own passages in a scratch database rather than reading the dev
corpus, so the assertions are about the filter rather than about whatever
happens to be ingested.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa

from logos.filters import ScriptureRefRangeFilter

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.integration]

#: `metadata` is deliberately written as `json`, matching migration 001. Writing
#: it as `jsonb` here would make the suite pass against a schema the engine does
#: not have, which is exactly the mistake being corrected.
REFS = {
    "in-genesis": ["Gen 1:1", "Gen 2:4"],
    "in-romans": ["Rom 3:23"],
    "empty-list": [],
    "wrong-shape": "Gen 1:1",  # a bare string where an array is expected
    "absent": None,  # no scripture_refs key at all
}


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    from sqlalchemy.ext.asyncio import create_async_engine

    from research_engine.testing import ensure_test_database, resolve_test_db_url

    url = resolve_test_db_url()
    if not await ensure_test_database(url):
        pytest.skip("no Postgres reachable for the filter execution suite")
    eng = create_async_engine(url, pool_pre_ping=True)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
async def passages(engine: AsyncEngine) -> AsyncIterator[dict[str, uuid.UUID]]:
    """One passage per shape in REFS, removed again afterwards."""
    document_id = uuid.uuid4()
    ids = {name: uuid.uuid4() for name in REFS}
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO core.documents "
                "(id, title, document_type, source, content_hash, parser, parser_version) "
                "VALUES (:id, 'filter fixture', 'book', 'test://filters', "
                "        '\\x00'::bytea, 'test', '1')"
            ),
            {"id": document_id},
        )
        for name, refs in REFS.items():
            meta = {} if refs is None else {"scripture_refs": refs}
            await conn.execute(
                sa.text(
                    "INSERT INTO core.passages "
                    "(id, document_id, text, position, chunker, chunker_version, "
                    " metadata, locator, content_hash) "
                    "VALUES (:id, :doc, :text, :pos, 'test', '1', "
                    "        CAST(:meta AS json), '{}'::json, :hash)"
                ),
                {
                    "id": ids[name],
                    "doc": document_id,
                    "text": f"passage {name}",
                    "pos": list(REFS).index(name),
                    "meta": json.dumps(meta),
                    "hash": hashlib.sha256(f"passage {name}".encode()).digest(),
                },
            )
    try:
        yield ids
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                sa.text("DELETE FROM core.documents WHERE id = :id"), {"id": document_id}
            )


async def _matching(engine: AsyncEngine, value: dict) -> set[uuid.UUID]:
    clause = ScriptureRefRangeFilter().build_clause(value)
    async with engine.connect() as conn:
        rows = await conn.execute(sa.select(clause.subquery()))
    return {row[0] for row in rows}


class TestTheFilterExecutes:
    async def test_a_range_query_runs_against_a_json_metadata_column(
        self, engine: AsyncEngine, passages: dict[str, uuid.UUID]
    ) -> None:
        """The regression. Before the cast this raised rather than returning."""
        matched = await _matching(engine, {"start": "Gen 1:1", "end": "Gen 3:24"})
        assert passages["in-genesis"] in matched

    async def test_a_passage_outside_the_range_is_not_matched(
        self, engine: AsyncEngine, passages: dict[str, uuid.UUID]
    ) -> None:
        matched = await _matching(engine, {"start": "Gen 1:1", "end": "Gen 3:24"})
        assert passages["in-romans"] not in matched

    async def test_a_single_reference_defaults_its_end(
        self, engine: AsyncEngine, passages: dict[str, uuid.UUID]
    ) -> None:
        matched = await _matching(engine, {"start": "Rom 3"})
        assert passages["in-romans"] in matched
        assert passages["in-genesis"] not in matched

    async def test_a_spelled_out_book_name_is_accepted_as_input(
        self, engine: AsyncEngine, passages: dict[str, uuid.UUID]
    ) -> None:
        """The two vocabularies meet on the *query* side, not the stored side.

        Every one of the 52,579 refs stored in this corpus is an SBL
        abbreviation ("2 Cor 5:1"), so the LIKE patterns are built from
        abbreviations and that is correct. What a caller types is another
        matter — locators elsewhere in the corpus spell books out — so
        `ref_to_ordinal` accepts "Genesis 1:1" and resolves it to the same
        range as "Gen 1:1". This asserts the two agree end to end.
        """
        spelled = await _matching(engine, {"start": "Genesis 1:1", "end": "Genesis 3:24"})
        abbreviated = await _matching(engine, {"start": "Gen 1:1", "end": "Gen 3:24"})
        assert spelled == abbreviated
        assert passages["in-genesis"] in spelled


class TestShapesThatMustNotAbortTheScan:
    """A row whose metadata is not what the filter expects must not raise.

    `jsonb_array_elements_text` on a non-array is an error, not an empty set, so
    one badly-shaped passage anywhere in the corpus would take down every search
    that used this filter. The clause guards on `jsonb_typeof` for that reason.
    """

    @pytest.mark.parametrize("shape", ["wrong-shape", "empty-list", "absent"])
    async def test_a_passage_with_unusable_refs_is_skipped_not_fatal(
        self, engine: AsyncEngine, passages: dict[str, uuid.UUID], shape: str
    ) -> None:
        matched = await _matching(engine, {"start": "Gen 1:1", "end": "Rev 22:21"})
        assert passages[shape] not in matched
        # And the query still found the rows it should have.
        assert passages["in-genesis"] in matched

    async def test_a_book_with_no_matching_passage_returns_empty(
        self, engine: AsyncEngine, passages: dict[str, uuid.UUID]
    ) -> None:
        assert await _matching(engine, {"start": "Obad 1", "end": "Obad 1"}) == set()
