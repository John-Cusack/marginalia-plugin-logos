"""Recover page numbers for books already in the corpus.

The re-chunk path dropped every page marker: markers come from the Logos HTML,
re-chunking works from stored markdown, and nothing carried them across. The
result was 67,730 passages and not one locator — a corpus you could search but
not cite.

Re-walking Logos to get them back would be days of API calls. It is not
necessary. The walk staged its chunks, 251,768 of them are still in
`logos_ingest_chunks`, and 30,249 carry the page they began on. That is enough
to reconstruct the marker list per article and hand it to the same
`page_locator` ingestion uses.

This updates `passages.locator` and nothing else. A locator is derived from the
source rather than from the text, so learning it late invalidates nothing — not
the chunk, not its offsets, not its embedding. Re-ingesting TDNT to add page
numbers would re-embed 25,852 passages to change one JSON column on each.
"""

from __future__ import annotations

from bisect import bisect_right
from typing import TYPE_CHECKING, Any

import structlog

from logos.db.queries import (
    get_article_page_markers,
    get_ordered_article_texts,
)
from logos.tools.ingest_book import _assemble_book, page_locator

if TYPE_CHECKING:
    from uuid import UUID

logger = structlog.get_logger()


#: Passages sampled to confirm a document's offsets address the assembled text.
#: Cheap, and it is the whole safety argument — see `_offsets_match`.
VERIFY_SAMPLES = 5


async def backfill_pages(
    resource_id: str,
    *,
    passages: Any,
    documents: Any,
    dry_run: bool = False,
) -> list[dict]:
    """Attach page locators to a stored resource's passages.

    Returns one result per document, because a resource is not always one
    document: books walked before the whole-book rewrite are stored as a
    document per batch of articles.

    Markers are applied *per article*. Rebasing them all into one book-wide list
    would let a passage in an article with no markers inherit the last page of
    the article before it — a page number that is wrong rather than absent, and
    absent is the honest answer.
    """
    stored_docs = await documents.find_by_metadata("resource_id", resource_id)
    if not stored_docs:
        return [_result(resource_id, None, 0, 0, 0, dry_run, "not stored in the corpus")]

    markers_by_article = await get_article_page_markers(resource_id)
    if not markers_by_article:
        return [
            _result(resource_id, None, 0, 0, 0, dry_run,
                    "no staged chunk for this resource recorded a page")
        ]

    articles = await get_ordered_article_texts(resource_id)
    if not articles:
        return [
            _result(resource_id, None, 0, 0, 0, dry_run,
                    "no stored article text, so article spans cannot be reconstructed")
        ]
    assembled, spans = _assemble_book(articles)

    intervals = sorted(
        (spans[aid][0], spans[aid][1], aid) for aid, _ in articles if aid in spans
    )
    starts = [start for start, _, _ in intervals]

    rebased: dict[str, list[dict]] = {}
    for article_id, markers in markers_by_article.items():
        if article_id not in spans:
            continue
        offset = spans[article_id][0]
        rebased[article_id] = [
            {**m, "char_position": m["char_position"] + offset} for m in markers
        ]

    results = []
    for document in stored_docs:
        results.append(
            await _backfill_document(
                resource_id, document, assembled, intervals, starts, rebased,
                passages=passages, dry_run=dry_run,
            )
        )
    return results


async def _backfill_document(
    resource_id: str,
    document: Any,
    assembled: str,
    intervals: list[tuple[int, int, str]],
    starts: list[int],
    rebased: dict[str, list[dict]],
    *,
    passages: Any,
    dry_run: bool,
) -> dict:
    stored = await passages.get_by_document(document.id)
    if not stored:
        return _result(resource_id, document.id, 0, 0, len(rebased), dry_run,
                       "document has no passages")

    if not _offsets_match(stored, assembled):
        # Refusing is the point. A document stored per batch of articles carries
        # batch-relative offsets, so book-relative markers would land on the
        # wrong article and stamp confident, wrong page numbers — strictly worse
        # than none, because nothing downstream could tell they were wrong.
        return _result(
            resource_id, document.id, 0, len(stored), len(rebased), dry_run,
            "offsets do not address the assembled book (legacy per-batch "
            "document); re-ingest it to get pages",
        )

    updates: list[tuple[UUID, dict]] = []
    for passage in stored:
        index = bisect_right(starts, passage.char_start) - 1
        if index < 0:
            continue
        _, end, article_id = intervals[index]
        # A passage straddling into the next article still belongs to the one it
        # starts in; anything outside every article is left alone.
        if passage.char_start >= end:
            continue
        markers = rebased.get(article_id)
        if not markers:
            continue
        locator = page_locator(markers, passage.char_start, passage.char_end)
        if locator and locator != passage.locator:
            updates.append((passage.id, {**passage.locator, **locator}))

    if not dry_run and updates:
        await passages.set_locators(updates)

    logger.info(
        "logos_pages_backfilled",
        resource_id=resource_id,
        document_id=str(document.id),
        located=len(updates),
        total=len(stored),
        dry_run=dry_run,
    )
    return _result(resource_id, document.id, len(updates), len(stored),
                   len(rebased), dry_run, "ok")


def _offsets_match(stored: list, assembled: str) -> bool:
    """Do these passages' offsets actually address *assembled*?

    Checked by reading the text back rather than by comparing lengths: two
    different assemblies can be the same size, and the failure this guards
    against is silent. Samples are spread through the document because a
    per-batch document agrees with the book for its first article and diverges
    after.
    """
    if not stored:
        return False
    step = max(len(stored) // VERIFY_SAMPLES, 1)
    for passage in stored[::step][:VERIFY_SAMPLES]:
        if assembled[passage.char_start : passage.char_end] != passage.text:
            return False
    return True


def _result(
    resource_id: str,
    document_id: UUID | None,
    located: int,
    total: int,
    articles: int,
    dry_run: bool,
    status: str,
) -> dict:
    return {
        "resource_id": resource_id,
        "document_id": str(document_id) if document_id else None,
        "passages_located": located,
        "passages_total": total,
        "articles_with_pages": articles,
        "coverage": round(located / total, 3) if total else 0.0,
        "dry_run": dry_run,
        "status": status,
    }
