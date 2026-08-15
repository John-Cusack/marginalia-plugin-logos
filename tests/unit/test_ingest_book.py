"""Tests for the two-phase ingest_book pipeline."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from research_engine.domain.passages import PassageDraft


@pytest.fixture(autouse=True)
def _no_article_text_lookup():
    """Keep unit tests off the database.

    `_store_with_retry` now looks up article text to rebase batch offsets.
    Returning nothing makes it skip rebasing, which is the behaviour these
    tests were written against; the rebasing tests below patch this themselves.
    """
    with patch(
        "logos.tools.ingest_book.get_article_texts",
        new_callable=AsyncMock,
        return_value={},
    ):
        yield


# ── PassageDraft round-trip serialization ─────────────────────────────────────


def _make_draft(
    position: int = 0, text: str = "Test passage", char_start: int = 0
) -> PassageDraft:
    return PassageDraft(
        position=position,
        char_start=char_start,
        char_end=char_start + len(text),
        text=text,
        token_count=len(text) // 4,
        chunker="verse_boundary",
        chunker_version="2.0",
        metadata={"scripture_refs": ["Gen 1:1"], "article_id": "ART1"},
    )


def test_passage_draft_round_trip():
    """PassageDraft survives model_dump → JSON → model_validate."""
    original = _make_draft(text="For God so loved the world (John 3:16).")
    serialized = original.model_dump()
    json_str = json.dumps(serialized)
    deserialized = PassageDraft(**json.loads(json_str))

    assert deserialized.text == original.text
    assert deserialized.position == original.position
    assert deserialized.token_count == original.token_count
    assert deserialized.chunker == original.chunker
    assert deserialized.chunker_version == original.chunker_version
    assert deserialized.metadata == original.metadata
    assert deserialized.locator == original.locator


def test_passage_draft_round_trip_empty_metadata():
    """PassageDraft with no metadata survives round-trip."""
    original = PassageDraft(
        position=0, char_start=0, char_end=9, text="Bare text", token_count=2,
        chunker="verse_boundary", chunker_version="2.0",
    )
    restored = PassageDraft(**json.loads(json.dumps(original.model_dump())))
    assert restored.metadata == {}
    assert restored.locator == {}


# ── Batch key assignment ──────────────────────────────────────────────────────


def test_batch_key_assignment():
    """Batch keys roll over at STORE_BATCH_SIZE boundaries."""
    from logos.tools.ingest_book import STORE_BATCH_SIZE

    # Simulate the batch key logic from _walk_and_checkpoint
    batch_number = 0
    batch_chunk_count = 0
    keys_assigned: list[str] = []

    for i in range(STORE_BATCH_SIZE * 3 + 10):
        key = f"b{batch_number:04d}"
        keys_assigned.append(key)
        batch_chunk_count += 1
        if batch_chunk_count >= STORE_BATCH_SIZE:
            batch_number += 1
            batch_chunk_count = 0

    assert keys_assigned[0] == "b0000"
    assert keys_assigned[STORE_BATCH_SIZE - 1] == "b0000"
    assert keys_assigned[STORE_BATCH_SIZE] == "b0001"
    assert keys_assigned[STORE_BATCH_SIZE * 2] == "b0002"
    assert keys_assigned[STORE_BATCH_SIZE * 3] == "b0003"


# ── _store_with_retry ─────────────────────────────────────────────────────────


def _make_chunk_row(chunk_id: int, position: int = 0, article_id: str = "ART1") -> dict:
    draft = _make_draft(position=position, text=f"Passage {chunk_id}")
    return {
        "id": chunk_id,
        "article_id": article_id,
        "draft_json": draft.model_dump(),
    }


@pytest.mark.asyncio
async def test_store_with_retry_success():
    """Successful store marks chunks as stored."""
    from logos.tools.ingest_book import _store_with_retry

    doc_id = uuid4()
    mock_ingestion = AsyncMock()
    mock_ingestion.ingest_drafts.return_value = {
        "document_id": str(doc_id),
        "passage_count": 3,
    }

    rows = [_make_chunk_row(i) for i in range(3)]

    with patch("logos.tools.ingest_book.mark_chunks_stored", new_callable=AsyncMock) as mock_stored:
        result = await _store_with_retry(
            "test-resource", "b0000", rows, mock_ingestion,
            {"resource_id": "test-resource"}, "Test Book",
        )

    assert result["stored"] == 3
    assert result["failed"] == 0
    mock_ingestion.ingest_drafts.assert_called_once()
    mock_stored.assert_called_once_with("test-resource", "b0000", doc_id)


@pytest.mark.asyncio
async def test_store_with_retry_halves_on_failure():
    """On failure, batch is halved and each half retried."""
    from logos.tools.ingest_book import _store_with_retry

    doc_id = uuid4()
    call_count = 0

    async def mock_ingest_drafts(**kwargs):
        nonlocal call_count
        call_count += 1
        drafts = kwargs["passage_drafts"]
        # Fail the first (large) batch, succeed on halves
        if len(drafts) > 5:
            raise RuntimeError("CUDA out of memory")
        return {"document_id": str(doc_id), "passage_count": len(drafts)}

    mock_ingestion = AsyncMock()
    mock_ingestion.ingest_drafts.side_effect = mock_ingest_drafts

    rows = [_make_chunk_row(i) for i in range(10)]

    with (
        patch("logos.tools.ingest_book.mark_chunks_stored", new_callable=AsyncMock),
        patch("logos.tools.ingest_book.mark_chunks_failed", new_callable=AsyncMock),
        patch("logos.tools.ingest_book.reassign_chunk_batch_keys", new_callable=AsyncMock),
    ):
        result = await _store_with_retry(
            "test-resource", "b0000", rows, mock_ingestion,
            {"resource_id": "test-resource"}, "Test Book",
        )

    assert result["stored"] == 10
    assert result["failed"] == 0
    # 1 initial attempt + 2 halves
    assert call_count == 3


@pytest.mark.asyncio
async def test_store_with_retry_permanent_failure():
    """Small batch that fails is marked permanently failed."""
    from logos.tools.ingest_book import _store_with_retry

    mock_ingestion = AsyncMock()
    mock_ingestion.ingest_drafts.side_effect = RuntimeError("CUDA out of memory")

    rows = [_make_chunk_row(i) for i in range(3)]  # Below MIN_BATCH_SIZE

    with patch("logos.tools.ingest_book.mark_chunks_failed", new_callable=AsyncMock) as mock_failed:
        result = await _store_with_retry(
            "test-resource", "b0000", rows, mock_ingestion,
            {"resource_id": "test-resource"}, "Test Book",
        )

    assert result["stored"] == 0
    assert result["failed"] == 3
    mock_failed.assert_called_once()


@pytest.mark.asyncio
async def test_store_with_retry_max_depth():
    """Recursion stops at MAX_RETRY_DEPTH even with large batches."""
    from logos.tools.ingest_book import MAX_RETRY_DEPTH, _store_with_retry

    mock_ingestion = AsyncMock()
    mock_ingestion.ingest_drafts.side_effect = RuntimeError("CUDA out of memory")

    # Large batch but at max depth already
    rows = [_make_chunk_row(i) for i in range(50)]

    with patch("logos.tools.ingest_book.mark_chunks_failed", new_callable=AsyncMock) as mock_failed:
        result = await _store_with_retry(
            "test-resource", "b0000", rows, mock_ingestion,
            {"resource_id": "test-resource"}, "Test Book",
            depth=MAX_RETRY_DEPTH,
        )

    assert result["stored"] == 0
    assert result["failed"] == 50
    mock_failed.assert_called_once()


@pytest.mark.asyncio
async def test_store_with_retry_recursive_halving():
    """Batch halves multiple times until pieces are small enough to succeed."""
    from logos.tools.ingest_book import _store_with_retry

    doc_id = uuid4()

    async def mock_ingest_drafts(**kwargs):
        drafts = kwargs["passage_drafts"]
        # Fail if more than 12 passages (forces 2 levels of halving from 50)
        if len(drafts) > 12:
            raise RuntimeError("CUDA out of memory")
        return {"document_id": str(doc_id), "passage_count": len(drafts)}

    mock_ingestion = AsyncMock()
    mock_ingestion.ingest_drafts.side_effect = mock_ingest_drafts

    rows = [_make_chunk_row(i) for i in range(50)]

    with (
        patch("logos.tools.ingest_book.mark_chunks_stored", new_callable=AsyncMock),
        patch("logos.tools.ingest_book.mark_chunks_failed", new_callable=AsyncMock),
        patch("logos.tools.ingest_book.reassign_chunk_batch_keys", new_callable=AsyncMock),
    ):
        result = await _store_with_retry(
            "test-resource", "b0000", rows, mock_ingestion,
            {"resource_id": "test-resource"}, "Test Book",
        )

    # All 50 should eventually be stored through recursive halving
    assert result["stored"] == 50
    assert result["failed"] == 0


# ── _assign_pages ────────────────────────────────────────────────────────────


def test_assign_pages_basic():
    """Page markers are assigned to drafts based on char offsets."""
    from logos.tools.ingest_book import _assign_pages

    drafts = [
        _make_draft(position=0, text="First chunk"),
        _make_draft(position=1, text="Second chunk"),
    ]
    drafts[0].char_start, drafts[0].char_end = 0, 50
    drafts[1].char_start, drafts[1].char_end = 60, 120

    markers = [
        {"raw_ref": "vp.1.18", "char_position": 0, "volume": "1", "page": "18"},
        {"raw_ref": "vp.1.19", "char_position": 55, "volume": "1", "page": "19"},
    ]

    _assign_pages(drafts, markers)

    assert drafts[0].metadata["page_start"] == "18"
    assert drafts[0].metadata["page_end"] == "18"
    assert drafts[0].metadata["volume"] == "1"
    assert drafts[0].metadata["page_refs"] == ["vp.1.18"]

    assert drafts[1].metadata["page_start"] == "19"
    assert drafts[1].metadata["page_end"] == "19"
    assert drafts[1].metadata["page_refs"] == ["vp.1.19"]


def test_assign_pages_spanning_two_pages():
    """A chunk that spans two page markers gets different start/end."""
    from logos.tools.ingest_book import _assign_pages

    drafts = [_make_draft(position=0, text="Long chunk spanning pages")]
    drafts[0].char_start, drafts[0].char_end = 0, 200

    markers = [
        {"raw_ref": "vp.1.18", "char_position": 0, "volume": "1", "page": "18"},
        {"raw_ref": "vp.1.19", "char_position": 100, "volume": "1", "page": "19"},
    ]

    _assign_pages(drafts, markers)

    assert drafts[0].metadata["page_start"] == "18"
    assert drafts[0].metadata["page_end"] == "19"
    assert drafts[0].metadata["page_refs"] == ["vp.1.18", "vp.1.19"]


def test_assign_pages_no_markers():
    """No markers means metadata is unchanged."""
    from logos.tools.ingest_book import _assign_pages

    drafts = [_make_draft(position=0, text="Some text")]
    original_metadata = dict(drafts[0].metadata)

    _assign_pages(drafts, [])

    assert drafts[0].metadata == original_metadata


def test_assign_pages_roman_numerals():
    """Roman numeral pages are preserved as strings."""
    from logos.tools.ingest_book import _assign_pages

    drafts = [_make_draft(position=0, text="Front matter")]
    drafts[0].char_start, drafts[0].char_end = 0, 50

    markers = [
        {"raw_ref": "page.vii", "char_position": 0, "page": "vii"},
    ]

    _assign_pages(drafts, markers)

    assert drafts[0].metadata["page_start"] == "vii"
    assert drafts[0].metadata["page_end"] == "vii"
    assert "volume" not in drafts[0].metadata


# ── _next_toc_article ────────────────────────────────────────────────────────


def test_next_toc_article_found():
    """Returns the next TOC article after a failed one."""
    from logos.tools.ingest_book import _next_toc_article

    toc_ids = ["TITLE", "AB.ALC", "AB.ALC.COM", "AB.ALC.DEF", "AB.ALC.GHI"]
    assert _next_toc_article(toc_ids, "AB.ALC.COM") == "AB.ALC.DEF"


def test_next_toc_article_last():
    """Returns None when the failed article is the last in TOC."""
    from logos.tools.ingest_book import _next_toc_article

    toc_ids = ["TITLE", "AB.ALC.COM"]
    assert _next_toc_article(toc_ids, "AB.ALC.COM") is None


def test_next_toc_article_not_in_toc():
    """Returns None when the failed article isn't in the TOC."""
    from logos.tools.ingest_book import _next_toc_article

    toc_ids = ["TITLE", "AB.ALC", "AB.ALC.DEF"]
    assert _next_toc_article(toc_ids, "AB.ALC.COM") is None


# ── URL → resourceId resolution ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_url_extracts_lls_id():
    """Resolver returns the LLS: id embedded in product page HTML."""
    from logos.tools import ingest_book

    page = '<html><body><a href="logosres:LLS:BLSSDRPCMKRTHLF">Open</a></body></html>'

    class _Resp:
        text = page
        def raise_for_status(self): pass

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, *a, **kw): return _Resp()

    with patch.object(ingest_book.httpx, "AsyncClient", lambda *a, **kw: _Client()):
        rid = await ingest_book._resolve_url_to_resource_id(
            "https://www.logos.com/product/248390/blessed-are-the-peacemakers"
        )
    assert rid == "LLS:BLSSDRPCMKRTHLF"


@pytest.mark.asyncio
async def test_resolve_url_rejects_non_logos_host():
    """Resolver refuses URLs outside logos.com to avoid SSRF surprises."""
    from logos.tools.ingest_book import _resolve_url_to_resource_id

    with pytest.raises(ValueError, match="Not a logos.com URL"):
        await _resolve_url_to_resource_id("https://evil.example.com/product/1")


@pytest.mark.asyncio
async def test_resolve_url_no_match_raises():
    """Resolver raises a clear error when the page lacks an LLS: id."""
    from logos.tools import ingest_book

    class _Resp:
        text = "<html><body>Nothing useful here</body></html>"
        def raise_for_status(self): pass

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, *a, **kw): return _Resp()

    with patch.object(ingest_book.httpx, "AsyncClient", lambda *a, **kw: _Client()):
        with pytest.raises(ValueError, match="Could not find an LLS:"):
            await ingest_book._resolve_url_to_resource_id(
                "https://www.logos.com/product/0/missing"
            )


@pytest.mark.asyncio
async def test_resolve_url_picks_first_when_multiple():
    """When several LLS: ids appear, resolver picks the first unique match."""
    from logos.tools import ingest_book

    class _Resp:
        text = "first LLS:AAA111 then LLS:BBB222 and LLS:AAA111 again"
        def raise_for_status(self): pass

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, *a, **kw): return _Resp()

    with patch.object(ingest_book.httpx, "AsyncClient", lambda *a, **kw: _Client()):
        rid = await ingest_book._resolve_url_to_resource_id(
            "https://www.logos.com/product/1/x"
        )
    assert rid == "LLS:AAA111"


@pytest.mark.asyncio
async def test_handler_requires_resource_id_or_url():
    """Handler rejects calls with neither resource_id nor url."""
    from logos.tools.ingest_book import handler

    with pytest.raises(ValueError, match="Provide either resource_id or url"):
        await handler()


# ── Canonical batch-state resolver (§3.B) ────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_batch_state_empty_db():
    """No chunks for resource → fresh start at b0000."""
    from logos.tools import ingest_book

    with patch.object(ingest_book, "get_max_batch_summary",
                      AsyncMock(return_value=None)):
        state = await ingest_book._resolve_batch_state("LLS:X")
    assert state.next_batch_number == 0
    assert state.chunks_in_active == 0


@pytest.mark.asyncio
async def test_resolve_batch_state_partial_active_batch():
    """Last key has 50 pending chunks → resume into it at count=50."""
    from logos.tools import ingest_book

    with patch.object(
        ingest_book, "get_max_batch_summary",
        AsyncMock(return_value={"batch_key": "b0007", "count": 50, "any_stored": False}),
    ):
        state = await ingest_book._resolve_batch_state("LLS:X")
    assert state.next_batch_number == 7
    assert state.chunks_in_active == 50


@pytest.mark.asyncio
async def test_resolve_batch_state_sealed_active_batch():
    """Last key is full (>= STORE_BATCH_SIZE) → walker moves to next number."""
    from logos.tools import ingest_book

    with patch.object(
        ingest_book, "get_max_batch_summary",
        AsyncMock(return_value={"batch_key": "b0007", "count": 100, "any_stored": False}),
    ):
        state = await ingest_book._resolve_batch_state("LLS:X")
    assert state.next_batch_number == 8
    assert state.chunks_in_active == 0


@pytest.mark.asyncio
async def test_resolve_batch_state_includes_stored():
    """Any stored chunk in the highest batch ⇒ walker must move on."""
    from logos.tools import ingest_book

    with patch.object(
        ingest_book, "get_max_batch_summary",
        AsyncMock(return_value={"batch_key": "b0007", "count": 50, "any_stored": True}),
    ):
        state = await ingest_book._resolve_batch_state("LLS:X")
    assert state.next_batch_number == 8
    assert state.chunks_in_active == 0


@pytest.mark.asyncio
async def test_resolve_batch_state_handles_halved_keys():
    """A halved key like 'b0007a' is parsed for its leading digits."""
    from logos.tools import ingest_book

    with patch.object(
        ingest_book, "get_max_batch_summary",
        AsyncMock(return_value={"batch_key": "b0007a", "count": 50, "any_stored": False}),
    ):
        state = await ingest_book._resolve_batch_state("LLS:X")
    assert state.next_batch_number == 7
    assert state.chunks_in_active == 50


def test_active_batch_key_format():
    """active_batch_key formats with 4-digit zero padding."""
    from logos.tools.ingest_book import BatchState, _active_batch_key

    assert _active_batch_key(BatchState(0, 0)) == "b0000"
    assert _active_batch_key(BatchState(7, 50)) == "b0007"
    assert _active_batch_key(BatchState(123, 0)) == "b0123"


# ── Walker try/finally sentinel emission (§3.A) ──────────────────────────────


@pytest.mark.asyncio
async def test_walker_emits_sentinel_on_clean_completion():
    """End-of-chain reached → None is the last (and only) item on the queue."""
    from logos.tools import ingest_book

    book_data = {
        "resourceTitle": "Test",
        "abbreviatedTitle": "T",
        "article": {"content": "", "articleId": "TITLE"},
        "nextArticleId": None,
    }
    queue: asyncio.Queue = asyncio.Queue()

    async def fake_get(path: str):
        if "tableofcontents" in path:
            return {"items": []}
        raise AssertionError(f"Unexpected GET {path} (book_data should be reused)")

    with (
        patch.object(ingest_book, "get_ingest_progress",
                     AsyncMock(return_value=None)),
        patch.object(ingest_book, "_resolve_batch_state",
                     AsyncMock(return_value=ingest_book.BatchState(0, 0))),
        patch.object(ingest_book.logos_client, "get",
                     AsyncMock(side_effect=fake_get)),
        patch.object(ingest_book, "upsert_ingest_progress", AsyncMock()),
        patch.object(ingest_book, "insert_chunks", AsyncMock()),
    ):
        result = await ingest_book._walk_and_checkpoint(
            "LLS:TEST", book_data=book_data, batch_queue=queue,
        )

    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    assert items[-1] is None
    assert result["walk_status"] == "complete"


@pytest.mark.asyncio
async def test_walker_emits_sentinel_on_exception():
    """If the impl raises, the wrapper's finally still emits the sentinel."""
    from logos.tools import ingest_book

    queue: asyncio.Queue = asyncio.Queue()

    with patch.object(
        ingest_book, "get_ingest_progress",
        AsyncMock(side_effect=RuntimeError("DB exploded")),
    ):
        with pytest.raises(RuntimeError, match="DB exploded"):
            await ingest_book._walk_and_checkpoint(
                "LLS:TEST", batch_queue=queue,
            )

    assert queue.qsize() == 1
    assert queue.get_nowait() is None


@pytest.mark.asyncio
async def test_walker_emits_sentinel_on_cancellation():
    """If the walker task is cancelled, the wrapper's finally still emits the sentinel."""
    from logos.tools import ingest_book

    queue: asyncio.Queue = asyncio.Queue()
    started = asyncio.Event()

    async def hang_get(*a, **k):
        started.set()
        await asyncio.sleep(60)  # never returns within the test

    with (
        patch.object(ingest_book, "get_ingest_progress",
                     AsyncMock(return_value=None)),
        patch.object(ingest_book.logos_client, "get",
                     AsyncMock(side_effect=hang_get)),
    ):
        task = asyncio.create_task(
            ingest_book._walk_and_checkpoint("LLS:TEST", batch_queue=queue),
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert queue.qsize() == 1
    assert queue.get_nowait() is None


@pytest.mark.asyncio
async def test_enqueue_or_abort_no_sibling_just_puts():
    """With no sibling, _enqueue_or_abort is a plain queue.put."""
    from logos.tools.ingest_book import _enqueue_or_abort

    queue: asyncio.Queue = asyncio.Queue()
    await _enqueue_or_abort(queue, "b0000", sibling=None)
    assert queue.get_nowait() == "b0000"


@pytest.mark.asyncio
async def test_enqueue_or_abort_aborts_when_sibling_done():
    """If the sibling task is done and queue stays full, abort within ~1s."""
    from logos.tools.ingest_book import _enqueue_or_abort

    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    queue.put_nowait("blocker")  # queue is now full

    async def already_done():
        return None

    sibling = asyncio.create_task(already_done())
    await sibling  # ensure sibling.done() is True

    with pytest.raises(RuntimeError, match="Storer terminated"):
        await asyncio.wait_for(
            _enqueue_or_abort(queue, "b0001", sibling=sibling),
            timeout=3.0,
        )


# ── Storer / _consume_and_store (§3.C, §3.D) ─────────────────────────────────


@pytest.mark.asyncio
async def test_consume_and_store_drains_iterable():
    """_consume_and_store processes every batch key fed to it."""
    from logos.tools import ingest_book

    progress = {"resource_title": "T", "abbreviated_title": "T", "authors": []}

    async def fake_store(resource_id, batch_key, chunk_rows, *a, **k):
        return {"stored": len(chunk_rows), "failed": 0, "batch_key": batch_key}

    with (
        patch.object(ingest_book, "get_chunks_for_batch",
                     AsyncMock(return_value=[{"id": 1, "draft_json": "{}"}])),
        patch.object(ingest_book, "get_ingest_progress",
                     AsyncMock(return_value=progress)),
        patch.object(ingest_book, "_store_with_retry",
                     AsyncMock(side_effect=fake_store)),
    ):
        result = await ingest_book._consume_and_store(
            "LLS:X", ingestion=object(), resource_title="T",
            batch_keys=ingest_book._aiter_list(["b0000", "b0001", "b0002"]),
        )

    assert result["batches_processed"] == 3
    assert result["total_stored"] == 3
    assert result["total_failed"] == 0


@pytest.mark.asyncio
async def test_storer_picks_up_authors_added_mid_walk():
    """Authors discovered between batches show up in later batch metadata.

    This is the §3.C bug: with v2's single-snapshot doc_metadata, the second
    batch would still see authors=[]. Per-batch progress refresh fixes it.
    """
    from logos.tools import ingest_book

    captured_metadata: list[dict] = []

    async def capture_store(resource_id, batch_key, chunk_rows, ingestion,
                            doc_metadata, resource_title, *a, **k):
        captured_metadata.append(dict(doc_metadata))
        return {"stored": 1, "failed": 0, "batch_key": batch_key}

    progress_responses = [
        {"resource_title": "T", "abbreviated_title": "T", "authors": []},
        {"resource_title": "T", "abbreviated_title": "T",
         "authors": ["Smith, J."]},
    ]
    it = iter(progress_responses)

    async def fake_progress(*a, **k):
        return next(it)

    with (
        patch.object(ingest_book, "get_chunks_for_batch",
                     AsyncMock(return_value=[{"id": 1, "draft_json": "{}"}])),
        patch.object(ingest_book, "get_ingest_progress",
                     AsyncMock(side_effect=fake_progress)),
        patch.object(ingest_book, "_store_with_retry",
                     AsyncMock(side_effect=capture_store)),
    ):
        await ingest_book._consume_and_store(
            "LLS:X", ingestion=object(), resource_title="T",
            batch_keys=ingest_book._aiter_list(["b0000", "b0001"]),
        )

    assert captured_metadata[0]["authors"] == []
    assert captured_metadata[1]["authors"] == ["Smith, J."]


@pytest.mark.asyncio
async def test_storer_aborts_on_store_timeout():
    """A hung _store_with_retry is aborted after STORE_BATCH_TIMEOUT_SEC and
    the batch is marked failed; the storer continues to the next batch."""
    from logos.tools import ingest_book

    progress = {"resource_title": "T", "abbreviated_title": "T", "authors": []}
    chunk_rows = [{"id": 1, "draft_json": "{}"}, {"id": 2, "draft_json": "{}"}]

    async def hang(*a, **k):
        await asyncio.sleep(60)

    mark_failed = AsyncMock(return_value=2)

    with (
        patch.object(ingest_book, "STORE_BATCH_TIMEOUT_SEC", 0.3),
        patch.object(ingest_book, "get_chunks_for_batch",
                     AsyncMock(return_value=chunk_rows)),
        patch.object(ingest_book, "get_ingest_progress",
                     AsyncMock(return_value=progress)),
        patch.object(ingest_book, "_store_with_retry",
                     AsyncMock(side_effect=hang)),
        patch.object(ingest_book, "mark_chunks_failed", mark_failed),
    ):
        result = await asyncio.wait_for(
            ingest_book._consume_and_store(
                "LLS:X", ingestion=object(), resource_title="T",
                batch_keys=ingest_book._aiter_list(["b0000"]),
            ),
            timeout=5.0,
        )

    assert result["total_stored"] == 0
    assert result["total_failed"] == 2
    assert mark_failed.await_count == 1
    err_msg = mark_failed.await_args.args[2]
    assert "timeout" in err_msg


@pytest.mark.asyncio
async def test_aiter_queue_yields_until_sentinel():
    """_aiter_queue returns all items up to the None sentinel, then stops."""
    from logos.tools.ingest_book import _aiter_queue

    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait("b0000")
    queue.put_nowait("b0001")
    queue.put_nowait(None)
    queue.put_nowait("b0002")  # this should never be yielded

    seen: list[str] = []
    async for k in _aiter_queue(queue):
        seen.append(k)

    assert seen == ["b0000", "b0001"]


# ── Pipelined handler path (§6.5) ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_preload_resume_queue_sorted_excluding_active():
    """Pre-existing pending keys are queued in lex order, minus active_key."""
    from logos.tools import ingest_book

    # Return them deliberately unsorted to confirm we sort.
    with patch.object(
        ingest_book, "get_pending_batch_keys",
        AsyncMock(return_value=["b0003", "b0001", "b0007", "b0002"]),
    ):
        queue: asyncio.Queue = asyncio.Queue()
        count = await ingest_book._preload_resume_queue(
            "LLS:X", queue, active_key="b0007",
        )

    assert count == 3
    drained = []
    while not queue.empty():
        drained.append(queue.get_nowait())
    assert drained == ["b0001", "b0002", "b0003"]


def test_normalize_results_passes_through_dicts():
    """If both gather results are dicts, they're returned unchanged."""
    from logos.tools.ingest_book import _normalize_results

    walk = {"walk_status": "complete"}
    store = {"store_status": "complete", "total_stored": 10, "total_failed": 0,
             "batches_processed": 1, "batch_results": []}
    w, s = _normalize_results(walk, store)
    assert w is walk
    assert s is store


def test_normalize_results_coerces_walker_exception():
    """Walker exception → dict with walk_status='errored' and the error message."""
    from logos.tools.ingest_book import _normalize_results

    err = RuntimeError("boom")
    store = {"store_status": "complete", "total_stored": 0, "total_failed": 0,
             "batches_processed": 0, "batch_results": []}
    w, s = _normalize_results(err, store)
    assert w["walk_status"] == "errored"
    assert "boom" in w["error"]
    assert s is store


def test_normalize_results_coerces_storer_exception():
    """Storer exception → dict with store_status='errored' and zero counts."""
    from logos.tools.ingest_book import _normalize_results

    walk = {"walk_status": "complete"}
    err = RuntimeError("db down")
    w, s = _normalize_results(walk, err)
    assert w is walk
    assert s["store_status"] == "errored"
    assert s["total_stored"] == 0
    assert s["total_failed"] == 0
    assert s["batches_processed"] == 0
    assert s["batch_results"] == []


@pytest.mark.asyncio
async def test_mop_up_retry_results_carry_attempt_field():
    """Each mop-up retry tags its batch_results entries with the attempt
    number, and counts merge correctly across attempts."""
    from logos.tools import ingest_book

    # Initial store_result has 5 failed → mop-up runs.
    store_result = {
        "store_status": "complete",
        "total_stored": 100,
        "total_failed": 5,
        "batches_processed": 1,
        "batch_results": [{"stored": 100, "failed": 5, "batch_key": "b0000"}],
    }

    # First attempt: stores 5 of the 5 retried-from-failed; total_failed → 0.
    retry_responses = [
        {"store_status": "complete", "total_stored": 5, "total_failed": 0,
         "batches_processed": 1,
         "batch_results": [{"stored": 5, "failed": 0, "batch_key": "b0000"}]},
    ]
    it = iter(retry_responses)

    async def fake_store_batches(*a, **k):
        return next(it)

    with (
        patch.object(ingest_book, "reset_failed_chunks", AsyncMock()),
        patch.object(ingest_book, "_store_batches",
                     AsyncMock(side_effect=fake_store_batches)),
    ):
        merged = await ingest_book._run_mop_up_retries(
            "LLS:X", ingestion=object(), store_result=store_result,
            sleep_seconds=0,
        )

    assert merged["total_stored"] == 105
    assert merged["total_failed"] == 0
    # Every retry-attempt entry has an `attempt` key.
    attempt_tagged = [r for r in merged["batch_results"] if "attempt" in r]
    assert len(attempt_tagged) == 1
    assert attempt_tagged[0]["attempt"] == 1


@pytest.mark.asyncio
async def test_mop_up_retry_stops_when_no_failures():
    """If total_failed is 0 going in, no retry runs at all."""
    from logos.tools import ingest_book

    store_result = {
        "total_stored": 50, "total_failed": 0,
        "batches_processed": 1, "batch_results": [],
    }
    fake_store = AsyncMock()
    with (
        patch.object(ingest_book, "_store_batches", fake_store),
        patch.object(ingest_book, "reset_failed_chunks", AsyncMock()),
    ):
        merged = await ingest_book._run_mop_up_retries(
            "LLS:X", ingestion=object(), store_result=store_result,
            sleep_seconds=0,
        )
    assert merged is store_result
    assert fake_store.await_count == 0


# ── Batch offset rebasing ─────────────────────────────────────────────────────
#
# A stored document is a batch of chunks from many articles, but the chunker
# addresses one article. Without rebasing, every article's first chunk claims
# offset 0 of the document and quote verification resolves to the wrong article.


def _row(chunk_id: int, article_id: str, text: str, char_start: int) -> dict:
    draft = _make_draft(position=0, text=text, char_start=char_start)
    return {"id": chunk_id, "article_id": article_id, "draft_json": draft.model_dump()}


@pytest.mark.asyncio
async def test_rebase_shifts_offsets_onto_the_batch_document():
    from logos.tools.ingest_book import ARTICLE_SEPARATOR, _rebase_offsets

    art_a, art_b = "Alpha article text.", "Beta article text here."
    rows = [
        _row(1, "A", "Alpha", 0),
        _row(2, "A", "article", 6),
        _row(3, "B", "Beta", 0),
    ]
    drafts = [PassageDraft(**r["draft_json"]) for r in rows]

    with patch(
        "logos.tools.ingest_book.get_article_texts",
        new_callable=AsyncMock,
        return_value={"A": art_a, "B": art_b},
    ):
        document_text = await _rebase_offsets("res", "b0000", rows, drafts)

    assert document_text == art_a + ARTICLE_SEPARATOR + art_b
    # Every draft now addresses the document it was stored in.
    for draft in drafts:
        assert document_text[draft.char_start : draft.char_end] == draft.text


@pytest.mark.asyncio
async def test_rebase_preserves_first_article_offsets():
    from logos.tools.ingest_book import _rebase_offsets

    rows = [_row(1, "A", "Alpha", 0)]
    drafts = [PassageDraft(**rows[0]["draft_json"])]

    with patch(
        "logos.tools.ingest_book.get_article_texts",
        new_callable=AsyncMock,
        return_value={"A": "Alpha article text."},
    ):
        await _rebase_offsets("res", "b0000", rows, drafts)

    assert drafts[0].char_start == 0


@pytest.mark.asyncio
async def test_rebase_returns_none_when_article_text_is_missing():
    """Chunks staged before article text was recorded must not be shifted by a
    guess: no canonical text is stored and the document is reported unanchorable.
    """
    from logos.tools.ingest_book import _rebase_offsets

    rows = [_row(1, "A", "Alpha", 0), _row(2, "B", "Beta", 0)]
    drafts = [PassageDraft(**r["draft_json"]) for r in rows]
    before = [(d.char_start, d.char_end) for d in drafts]

    with patch(
        "logos.tools.ingest_book.get_article_texts",
        new_callable=AsyncMock,
        return_value={"A": "Alpha article text."},
    ):
        document_text = await _rebase_offsets("res", "b0000", rows, drafts)

    assert document_text is None
    assert [(d.char_start, d.char_end) for d in drafts] == before


@pytest.mark.asyncio
async def test_repeated_article_ids_are_not_double_counted():
    """Chunks are ordered by id, so one article's chunks are consecutive."""
    from logos.tools.ingest_book import _rebase_offsets

    rows = [_row(1, "A", "Alpha", 0), _row(2, "A", "article", 6), _row(3, "B", "Beta", 0)]
    drafts = [PassageDraft(**r["draft_json"]) for r in rows]

    with patch(
        "logos.tools.ingest_book.get_article_texts",
        new_callable=AsyncMock,
        return_value={"A": "Alpha article text.", "B": "Beta article text here."},
    ) as mock_get:
        await _rebase_offsets("res", "b0000", rows, drafts)

    assert mock_get.call_args[0][1] == ["A", "B"]
