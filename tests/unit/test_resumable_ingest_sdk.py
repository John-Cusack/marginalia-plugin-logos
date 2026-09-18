"""A resumable ingest hands core SDK drafts that address the canonical text.

End to end through `logos.ingest_book`'s handler, with the Logos API and the
plugin's staging tables faked in memory and `RecordingIngestionClient` in place
of core. It refuses what core refuses on `ingest_drafts`: a draft that is not an
SDK `PassageDraft` whose text is exactly its slice of `full_text`, and a node
tree that core could not insert as given.

The walk is killed partway, as a dropped database connection kills it, then run
again; the embedding backend is then down for one store. Neither may lose,
duplicate or misplace a passage.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from unittest.mock import patch

import pytest
from research_engine_sdk import EmbeddingUnavailable, NodeDraft, PassageDraft

from logos.tools import ingest_book
from tests.fake_logos import ORDER, RESOURCE, TITLE, FakeLogos
from tests.unit.ingestion_double import RecordingIngestionClient

pytestmark = pytest.mark.unit

class StagingTables:
    """`logos_ingest_*` in memory, with the SQL's ordering and idempotence."""

    def __init__(self) -> None:
        self.progress: dict | None = None
        self.chunks: list[dict] = []
        self.texts: dict[str, str] = {}
        self.fail_insert_for: str | None = None

    async def get_ingest_progress(self, resource_id):
        return dict(self.progress) if self.progress else None

    async def upsert_ingest_progress(self, resource_id, title, abbreviated, last_article_id,
                                     index, total, walk_complete, authors):
        self.progress = {
            "resource_id": resource_id, "resource_title": title,
            "abbreviated_title": abbreviated, "last_article_id": last_article_id,
            "last_article_index": index, "total_articles": total,
            "walk_complete": walk_complete, "authors": authors,
        }

    async def save_article_text(self, resource_id, article_id, text):
        self.texts[article_id] = text

    async def insert_chunks(self, resource_id, article_id, batch_key, drafts):
        if article_id == self.fail_insert_for:
            self.fail_insert_for = None
            raise ConnectionError("connection to the database was lost")
        for draft in drafts:
            self.chunks.append({
                "id": len(self.chunks) + 1, "article_id": article_id,
                "batch_key": batch_key, "draft_json": json.dumps(draft),
                "status": "pending",
            })
        return len(drafts)

    async def get_max_batch_summary(self, resource_id):
        if not self.chunks:
            return None
        key = max(c["batch_key"] for c in self.chunks)
        rows = [c for c in self.chunks if c["batch_key"] == key]
        return {"batch_key": key, "count": len(rows),
                "any_stored": any(c["status"] == "stored" for c in rows)}

    async def get_all_pending_chunks(self, resource_id):
        return [c for c in self.chunks if c["status"] == "pending"]

    async def get_ordered_article_texts(self, resource_id):
        first_seen: dict[str, int] = {}
        for chunk in self.chunks:
            first_seen.setdefault(chunk["article_id"], chunk["id"])
        return [(a, self.texts[a]) for a in sorted(first_seen, key=first_seen.get)
                if a in self.texts]

    async def mark_resource_chunks_stored(self, resource_id, document_id):
        pending = [c for c in self.chunks if c["status"] == "pending"]
        for chunk in pending:
            chunk["status"] = "stored"
        return len(pending)


@pytest.fixture
def world():
    api, tables = FakeLogos(), StagingTables()
    with ExitStack() as stack:
        stack.enter_context(patch.object(ingest_book, "logos_client", api))
        for name in (
            "get_ingest_progress", "upsert_ingest_progress", "save_article_text",
            "insert_chunks", "get_max_batch_summary", "get_all_pending_chunks",
            "get_ordered_article_texts", "mark_resource_chunks_stored",
        ):
            stack.enter_context(patch.object(ingest_book, name, getattr(tables, name)))
        yield api, tables


async def test_a_walk_killed_midway_resumes_and_stores_canonical_slices(world) -> None:
    api, tables = world
    ingestion = RecordingIngestionClient()

    tables.fail_insert_for = "A.2.1"
    with pytest.raises(ConnectionError):
        await ingest_book.handler(resource_id=RESOURCE, ingestion=ingestion)
    assert tables.progress["last_article_id"] == "A.2", "checkpointed up to the crash"
    assert ingestion.calls == [], "nothing reaches core from a walk that did not finish"

    result = await ingest_book.handler(resource_id=RESOURCE, ingestion=ingestion)

    assert result["walk_status"] == "complete"
    assert result["resumed_from"] == "A.2"
    assert result["store_status"] == "complete"
    assert len(ingestion.calls) == 1, "one book, one document"

    call = ingestion.calls[0]
    drafts, full_text = call["passage_drafts"], call["full_text"]
    # The double already asserted every slice; these say what it means.
    assert all(isinstance(d, PassageDraft) for d in drafts)
    assert all(d.text == full_text[d.char_start : d.char_end] for d in drafts)
    assert result["total_stored"] == len(drafts) == len(tables.chunks)

    # Each article once, in book order — the resumed article was not staged twice.
    staged_articles = [c["article_id"] for c in tables.chunks]
    assert list(dict.fromkeys(staged_articles)) == ORDER
    offsets = [full_text.index(tables.texts[a]) for a in ORDER]
    assert offsets == sorted(offsets)
    assert all(full_text.count(tables.texts[a]) == 1 for a in ORDER)

    # Every passage lies inside the article it was chunked from.
    for draft in drafts:
        article = draft.metadata["article_id"]
        start = full_text.index(tables.texts[article])
        assert start <= draft.char_start <= draft.char_end <= start + len(tables.texts[article])

    nodes = call["node_drafts"]
    assert all(isinstance(n, NodeDraft) for n in nodes)
    assert nodes[0].title == TITLE
    child = next(n for n in nodes if n.metadata.get("article_id") == "A.2.1")
    parent = next(n for n in nodes if n.metadata.get("article_id") == "A.2")
    assert child.parent_path == parent.path, "A.2.1 nests under A.2"


async def test_an_embedding_outage_leaves_the_book_staged_for_the_next_run(world) -> None:
    api, tables = world
    ingestion = RecordingIngestionClient(
        fail_with=EmbeddingUnavailable("Cannot reach the embedding server")
    )

    first = await ingest_book.handler(resource_id=RESOURCE, ingestion=ingestion)
    assert first["store_status"] == "embedding_unavailable"
    assert all(c["status"] == "pending" for c in tables.chunks), "nothing marked stored"
    assert ingestion.documents == []

    fetched = len(api.fetched)
    second = await ingest_book.handler(resource_id=RESOURCE, ingestion=ingestion)

    assert second["walk_status"] == "already_complete"
    assert len(api.fetched) == fetched, "the store resumes without walking Logos again"
    assert second["store_status"] == "complete"
    assert len(ingestion.documents) == 1
    assert all(c["status"] == "stored" for c in tables.chunks)
