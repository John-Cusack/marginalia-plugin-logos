"""logos.ingest_book — Two-phase resilient book ingestion.

Phase 1: Walk the Logos article chain, checkpoint each article's PassageDrafts
          to the plugin DB (logos_ingest_chunks).  Resumable from any crash.
Phase 2: Read pending chunks from the DB, store to corpus via ingest_drafts()
          with adaptive batch-halving on failure.
"""

from __future__ import annotations

import asyncio
import json
from bisect import bisect_right
import re
import time
from collections.abc import AsyncGenerator, AsyncIterable, Iterable
from dataclasses import dataclass
from urllib.parse import quote, urlparse
from uuid import UUID

import httpx

from research_engine.domain.errors import EmbeddingUnavailable
from research_engine.domain.nodes import build_node_tree
from research_engine.domain.passages import PassageDraft
from research_engine.plugins.sdk import tool

from logos.db.migrate import run_migrations
from logos.db.queries import (
    get_article_page_markers,
    get_all_pending_chunks,
    get_article_metadata,
    get_article_texts,
    get_chunks_for_batch,
    get_ingest_progress,
    get_max_batch_summary,
    get_ordered_article_texts,
    get_pending_batch_keys,
    insert_chunks,
    mark_chunks_failed,
    mark_chunks_stored,
    mark_resource_chunks_stored,
    reassign_chunk_batch_keys,
    reset_failed_chunks,
    save_article_text,
    upsert_ingest_progress,
)
from logos.http.client import logos_client
from logos.ingest.chunker import VerseChunker
from logos.ingest.scripture_refs import extract_scripture_refs
from logos.ingest.toc_walker import TocOffsetIndex, walk_toc
from logos.lib.logger import log
from logos.parsers.html_to_markdown import html_to_markdown_with_refs

# ── Tunables ──────────────────────────────────────────────────────────────────

# Passages per ingest_drafts() call — keeps GPU memory well within bounds.
STORE_BATCH_SIZE = 100

# Retry settings for transient article-fetch failures.
#: How far back the walk will rewind looking for the first article. A book
#: root can sit deep inside a long reference work, and the rewind costs one
#: request per step, so it is bounded rather than open-ended.
_MAX_REWIND = 2_000

MAX_RETRIES = 3
RETRY_BASE_DELAY = 2  # seconds, doubled each retry

# Maximum recursive halving depth for store failures (100→50→25→12→6).
MAX_RETRY_DEPTH = 4

# Minimum batch size below which we stop halving and mark failed.
MIN_BATCH_SIZE = 5

# Matches Logos library resource IDs (e.g. "LLS:BLSSDRPCMKRTHLF").
_LLS_RE = re.compile(r"LLS:[A-Z0-9]+")

# Matches walker batch keys (e.g. "b0007"). Halving suffixes such as "b0007a"
# are tolerated; only the leading numeric portion is meaningful for resume.
_BATCH_KEY_DIGITS_RE = re.compile(r"^b(\d+)")


# ── Canonical batch-state resolver (resume + active-key derivation) ──────────


@dataclass(frozen=True)
class BatchState:
    """Where the walker should resume writing chunks.

    next_batch_number — the integer N for the batch_key f"b{N:04d}".
    chunks_in_active  — count already in that key (so the walker rolls to
                        the next batch the moment it adds enough new chunks).
    """

    next_batch_number: int
    chunks_in_active: int


async def _resolve_batch_state(resource_id: str) -> BatchState:
    """Single source of truth for resume + active-key protection.

    Considers both pending and stored chunks. If the highest-keyed batch is
    full (>= STORE_BATCH_SIZE) or already partly stored, the walker moves
    to the next number with a fresh count; otherwise it resumes appending
    into that batch.
    """
    summary = await get_max_batch_summary(resource_id)
    if summary is None:
        return BatchState(next_batch_number=0, chunks_in_active=0)
    m = _BATCH_KEY_DIGITS_RE.match(summary["batch_key"])
    n = int(m.group(1)) if m else 0
    if summary["any_stored"] or summary["count"] >= STORE_BATCH_SIZE:
        return BatchState(next_batch_number=n + 1, chunks_in_active=0)
    return BatchState(next_batch_number=n, chunks_in_active=summary["count"])


def _active_batch_key(state: BatchState) -> str:
    """Batch key the walker writes to next; storer pre-load excludes this."""
    return f"b{state.next_batch_number:04d}"


# ── Concurrency helpers (used by the pipelined ingest path) ──────────────────


async def _enqueue_or_abort(
    queue: asyncio.Queue,
    item,
    sibling: asyncio.Task | None = None,
) -> None:
    """Put `item` on `queue`, aborting if the sibling consumer task has died.

    With sibling=None this is just `await queue.put(item)`. With a sibling, we
    poll on the put with a 1s timeout and check sibling.done() between
    attempts, so a dead consumer can't deadlock the producer on a bounded
    queue (or on a Python deadlock that we'd rather notice quickly).
    """
    if sibling is None:
        await queue.put(item)
        return
    while True:
        try:
            await asyncio.wait_for(queue.put(item), timeout=1.0)
            return
        except TimeoutError:
            if sibling.done():
                raise RuntimeError("Storer terminated; walker aborting") from None


async def _aiter_queue(queue: asyncio.Queue) -> AsyncGenerator[str, None]:
    """Yield batch keys from the queue until a None sentinel arrives.

    task_done() in finally so it fires for every get() — including the
    sentinel path (no yield), and the case where the consumer raises and
    aclose() injects GeneratorExit through the yield.
    """
    while True:
        item = await queue.get()
        try:
            if item is None:
                return
            yield item
        finally:
            queue.task_done()


async def _aiter_list(items: Iterable[str]) -> AsyncGenerator[str, None]:
    """Adapt a sync iterable to an async generator for _consume_and_store."""
    for item in items:
        yield item


async def _fetch_book_data(resource_id: str) -> dict:
    """Single book-metadata fetch shared between handler and walker."""
    return await logos_client.get(f"/api/app/books/{quote(resource_id)}")


async def _preload_resume_queue(
    resource_id: str,
    queue: asyncio.Queue,
    active_key: str,
) -> int:
    """Queue pre-existing pending batches in sorted order, skipping the
    active key the walker will continue writing into.

    Returns the number of keys enqueued (handy for logging).
    """
    pending = sorted(await get_pending_batch_keys(resource_id))
    count = 0
    for k in pending:
        if k != active_key:
            queue.put_nowait(k)
            count += 1
    return count


# ── URL → resourceId resolution ───────────────────────────────────────────────


async def _resolve_url_to_resource_id(url: str) -> str:
    """Fetch a logos.com product page and extract its LLS: resourceId.

    The product page embeds the library resourceId in plain text, so we just
    fetch with an unauthenticated httpx client and regex it out.
    """
    host = urlparse(url).hostname or ""
    if not (host == "logos.com" or host.endswith(".logos.com")):
        raise ValueError(f"Not a logos.com URL: {url}")

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        response = await client.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        response.raise_for_status()

    matches = list(dict.fromkeys(_LLS_RE.findall(response.text)))
    if not matches:
        raise ValueError(
            f"Could not find an LLS: resourceId on {url}. "
            "The page format may have changed, or the URL may not be a "
            "product page."
        )
    if len(matches) > 1:
        log(f"Multiple resourceIds at {url}: {matches}; using first")
    return matches[0]


# ── Page-to-chunk assignment ─────────────────────────────────────────────────


def page_locator(markers: list[dict], char_start: int, char_end: int) -> dict:
    """The locator for one span, given a book's page markers.

    Shared by ingestion and by the backfill that recovers pages for books
    already in the corpus. They must agree: a passage should not get a different
    page depending on which code path last touched it.

    *markers* must be sorted by ``char_position`` and use the same origin as the
    span — article-relative during ingestion, book-relative in the backfill.
    """
    if not markers:
        return {}
    positions = [m["char_position"] for m in markers]
    first = max(bisect_right(positions, char_start) - 1, 0)
    last = max(bisect_right(positions, char_end) - 1, first)

    locator: dict = {
        "page_start": markers[first].get("page"),
        "page_end": markers[last].get("page"),
    }
    if markers[first].get("volume"):
        locator["volume"] = markers[first]["volume"]
    refs = [
        markers[i].get("raw_ref") for i in range(first, last + 1)
        if markers[i].get("raw_ref")
    ]
    if refs:
        locator["page_refs"] = refs
    return locator


def _assign_pages(
    drafts: list[PassageDraft],
    page_markers: list[dict],
) -> None:
    """Annotate each draft's locator with page_start/page_end from markers.

    `locator`, not `metadata`. PassageDraft documents locator as the home for
    "type-specific extras (page, verse, timecode) that are meaningful to a
    reader", and it is what search hits and verify_quote surface. Writing pages
    to metadata meant every consumer that asked a passage where it came from got
    an empty dict — which is why a corpus of 67,730 passages had no locators.

    Offsets come from the draft itself, not from `locator`. Reading the old
    locator keys here would silently return 0 for every chunk and stamp them all
    with the article's first page. Called before batch rebasing, so the spans are
    article-relative — which is also what page_markers' char_position values are.
    """
    if not page_markers or not drafts:
        return
    for draft in drafts:
        draft.locator.update(
            page_locator(page_markers, draft.char_start, draft.char_end)
        )


# ── TOC-based chain recovery ─────────────────────────────────────────────────


def _next_toc_article(toc_ids: list[str], failed_id: str) -> str | None:
    """Find the next article in the TOC after *failed_id*.

    If *failed_id* is in the TOC list, return the following entry.
    Otherwise return None (no recovery possible).
    """
    try:
        idx = toc_ids.index(failed_id)
    except ValueError:
        return None
    if idx + 1 < len(toc_ids):
        return toc_ids[idx + 1]
    return None


async def _first_article(resource_id: str, from_article_id: str | None) -> str | None:
    """Follow ``previousArticleId`` back to the article that has none.

    The book root hands back the reader's last-read position, so a walk that
    starts there silently omits everything before it. Only the article chain
    knows where a book begins: the first article is the one with no previous.

    Bounded by ``_MAX_REWIND`` and by a seen-set, so a cyclic or unbounded
    chain gives up and leaves the caller to start where it already was.
    """
    if not from_article_id:
        return None
    current = from_article_id
    seen: set[str] = set()
    for _ in range(_MAX_REWIND):
        if current in seen:
            log(f"Rewind hit a cycle at {current}; starting there")
            return current
        seen.add(current)
        try:
            data = await logos_client.get(
                f"/api/app/books/{quote(resource_id)}/articles/{quote(current)}"
            )
        except Exception as e:
            log(f"Rewind stopped at {current}: {e}")
            return current
        previous = data.get("previousArticleId")
        if previous is None:
            return current
        current = previous
    log(f"Rewind hit the {_MAX_REWIND}-article limit; starting at {current}")
    return current


async def _recover_by_parent(resource_id: str, failed_id: str) -> str | None:
    """Try trimming the last component of a failed article ID to find a parent.

    For example, if 'AB.ALC.COM' returns 403, try 'AB.ALC' which may succeed
    and provide a nextArticleId to continue the chain from.
    """
    parts = failed_id.rsplit(".", 1)
    while len(parts) == 2:
        parent_id = parts[0]
        try:
            art_data = await logos_client.get(
                f"/api/app/books/{quote(resource_id)}/articles/{quote(parent_id)}"
            )
            next_id = art_data.get("nextArticleId")
            if next_id and next_id != failed_id:
                log(f"Parent probe: {parent_id} → nextArticleId={next_id}")
                return next_id
        except Exception:
            pass
        parts = parent_id.rsplit(".", 1)
    return None


async def _recover_by_alpha_scan(
    resource_id: str, failed_id: str, visited: set[str] | None = None,
) -> str | None:
    """Scan forward alphabetically from a failed article's prefix.

    Builds candidates by advancing from the failed article's stem, trying
    both the stem with incremented last character and shortened two-letter
    prefixes. Returns the first article that exists.
    """
    # Extract the top-level prefix (e.g. "AB." from "AB.ALEXAND.COM")
    dot = failed_id.find(".")
    if dot < 0:
        return None
    prefix = failed_id[: dot + 1]

    # Get the stem from the failed ID's second component
    rest = failed_id[dot + 1:]
    second_dot = rest.find(".")
    stem = rest[:second_dot] if second_dot >= 0 else rest
    if not stem:
        return None

    candidates: list[str] = []

    # Strategy A: advance first letter (AL→B, B→C, ...) — broadest jump
    if len(stem) >= 1:
        fl = ord(stem[0].upper())
        for c in range(fl + 1, ord("Z") + 1):
            candidates.append(f"{prefix}{chr(c)}")

    # Strategy B: truncate stem short→long and advance last char
    # (ALEXAND → AM, AN, ...; then ALF, ALG, ...; then ALEXB, ...)
    for length in range(2, len(stem)):
        trunc = stem[:length].upper()
        last = ord(trunc[-1])
        base = trunc[:-1]
        for c in range(last + 1, ord("Z") + 1):
            candidates.append(f"{prefix}{base}{chr(c)}")

    # Strategy C: advance stem's last character (ALEXAND → ALEXANE, ...)
    if len(stem) >= 3:
        stem_base = stem[:-1].upper()
        last_char = ord(stem[-1].upper())
        for c in range(last_char + 1, ord("Z") + 1):
            candidates.append(f"{prefix}{stem_base}{chr(c)}")

    # Deduplicate while preserving order, cap at 30 probes
    seen: set[str] = set()
    unique: list[str] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            unique.append(c)
        if len(unique) >= 30:
            break

    for candidate in unique:
        if visited and candidate in visited:
            continue
        try:
            art_data = await logos_client.get(
                f"/api/app/books/{quote(resource_id)}/articles/{quote(candidate)}"
            )
            log(f"Alpha scan: {candidate} exists → "
                f"nextArticleId={art_data.get('nextArticleId')}")
            return candidate
        except Exception:
            continue
    return None


# ── Phase 1: Walk & Checkpoint ───────────────────────────────────────────────


async def _walk_and_checkpoint(
    resource_id: str,
    max_articles: int = 0,
    book_data: dict | None = None,
    batch_queue: asyncio.Queue | None = None,
    sibling: asyncio.Task | None = None,
) -> dict:
    """Phase 1 walker. The wrapper exists to guarantee the storer-shutdown
    sentinel is emitted on every exit path (clean return, exception,
    cancellation). See PIPELINED_INGEST.md §3.A.
    """
    try:
        return await _walk_impl(
            resource_id, max_articles, book_data, batch_queue, sibling,
        )
    finally:
        if batch_queue is not None:
            try:
                batch_queue.put_nowait(None)
            except asyncio.QueueFull:
                pass


async def _walk_impl(
    resource_id: str,
    max_articles: int = 0,
    book_data: dict | None = None,
    batch_queue: asyncio.Queue | None = None,
    sibling: asyncio.Task | None = None,
) -> dict:
    """Fetch articles from Logos API, chunk them, and checkpoint to the DB.

    If a prior walk was interrupted, resumes from the last checkpointed article.
    If the walk already completed, returns immediately.
    """
    # Check existing progress
    progress = await get_ingest_progress(resource_id)
    if progress and progress["walk_complete"]:
        log(f"Walk already complete for {resource_id} "
            f"({progress['total_articles']} articles)")
        return {
            "resource_id": resource_id,
            "resource_title": progress["resource_title"],
            "walk_status": "already_complete",
            "articles_walked": progress["total_articles"],
            "resumed_from": None,
        }

    # 1. Get resource metadata (or use the pre-fetched copy from the handler)
    if book_data is None:
        log(f"Fetching resource metadata for {resource_id}...")
        data = await logos_client.get(f"/api/app/books/{quote(resource_id)}")
    else:
        data = book_data
    resource_title = data.get("resourceTitle", resource_id)
    abbreviated = data.get("abbreviatedTitle", "")
    log(f"Resource: {resource_title} ({abbreviated})")

    # 2. Fetch TOC and build offset index for author/heading lookup
    toc_index: TocOffsetIndex | None = None
    toc_nodes: list = []
    toc_node_count = 0
    unique_authors: set[str] = set()
    try:
        toc_data = await logos_client.get(
            f"/api/app/books/{quote(resource_id)}/tableofcontents"
        )
        toc_nodes = walk_toc(toc_data)
        toc_index = TocOffsetIndex(toc_nodes)
        toc_node_count = len(toc_nodes)
        unique_authors = {n.author for n in toc_nodes if n.author}
        log(f"TOC walker produced {toc_node_count} nodes")
    except Exception as e:
        log(f"Failed to fetch/walk TOC: {e}")

    # Ordered TOC article IDs: the book's own running order. Used to choose
    # where to start, to recover from a broken chain, and to check afterwards
    # that the walk actually covered the book.
    toc_article_ids: list[str] = [n.id for n in toc_nodes if n.id] if toc_nodes else []

    # 3. Determine starting point (resume or fresh start)
    resumed_from: str | None = None
    article_index = 0
    preloaded_data: dict | None = None
    if progress:
        # Resume: skip ahead to the article *after* the last checkpointed one
        resumed_from = progress["last_article_id"]
        article_index = progress["last_article_index"]
        log(f"Resuming walk from article index {article_index} "
            f"(after {resumed_from})")
        # We need to fetch the last checkpointed article to get its nextArticleId
        try:
            art_data = await logos_client.get(
                f"/api/app/books/{quote(resource_id)}/articles/{quote(resumed_from)}"
            )
            article_id: str | None = art_data.get("nextArticleId")
        except Exception as e:
            log(f"Failed to fetch resume article {resumed_from}: {e}")
            article_id = None
    else:
        # Fresh start. The book root response (already fetched as `data`) has
        # the same shape as an article fetch and carries an article plus its
        # nextArticleId — but the article it carries is wherever the *reader*
        # last was, not the top of the book. Walking from there follows
        # nextArticleId to the end and stops, so everything before that point
        # is never fetched, and nothing fails: the walk reported "complete".
        #
        # *Four Views on Eternal Security* came in that way. The root pointed
        # at CH4.7.3.3, so the walk collected three sections and the glossary
        # — 22 articles, 15,826 characters of a whole book.
        #
        # `previousArticleId` is what settles it: the first article of a book
        # is the one that has none. The TOC cannot answer this, because its
        # entry IDs are not always article IDs — this book's are byte offsets
        # ("1~47461"), and starting from one returns 404.
        first_article = data.get("article") or {}
        root_article_id = first_article.get("articleId")
        article_id = root_article_id or "FIRST"
        if data.get("previousArticleId") is None:
            # The root really is the top of the book; reuse its payload.
            preloaded_data = data
        else:
            article_id = (
                await _first_article(resource_id, root_article_id) or root_article_id
            )
            log(f"Book root points at {root_article_id} (the last-read "
                f"position); walked back to {article_id} to start from the "
                f"beginning")

    # 4. Walk articles sequentially via nextArticleId chain
    chunker = VerseChunker()
    total_articles = article_index
    total_chunks_saved = 0
    # Canonical resume: derived from DB state, not article_index. Fixes the
    # legacy bug where article_index // STORE_BATCH_SIZE could re-use a
    # sealed batch_key on resume. See PIPELINED_INGEST.md §3.B.
    state = await _resolve_batch_state(resource_id)
    batch_number = state.next_batch_number
    batch_chunk_count = state.chunks_in_active
    failed_articles: list[str] = []
    visited: set[str] = set()
    skip_prefixes = ("IDX",)

    # Per-phase timing accumulators. Lets us decide whether `k=2` concurrent
    # fetch is worth the rate-limit risk: if process << fetch, the gain is
    # small. Reported in periodic logs and the return dict.
    fetch_elapsed = 0.0
    process_elapsed = 0.0

    while article_id:
        if max_articles > 0 and total_articles >= max_articles:
            break

        # Skip already-visited articles (prevents loops from recovery jumps)
        if article_id in visited:
            log(f"Already visited {article_id}, scanning forward...")
            recovered = await _recover_by_alpha_scan(
                resource_id, article_id, visited,
            )
            if recovered:
                log(f"Loop at {article_id}, recovered → {recovered}")
                article_id = recovered
                continue
            log(f"Loop at {article_id}, no unvisited articles found")
            break

        visited.add(article_id)

        # Skip index articles
        if any(article_id.startswith(p) for p in skip_prefixes):
            log(f"Skipping index article: {article_id}")
            try:
                art_data = await logos_client.get(
                    f"/api/app/books/{quote(resource_id)}/articles/{quote(article_id)}"
                )
                article_id = art_data.get("nextArticleId")
            except Exception:
                article_id = _next_toc_article(toc_article_ids, article_id)
            continue

        # Fetch article with retry + exponential backoff (or use the root
        # response we already fetched as the first article).
        art_data: dict | None = None
        if preloaded_data is not None:
            art_data = preloaded_data
            preloaded_data = None
        else:
            fetch_t0 = time.monotonic()
            for attempt in range(MAX_RETRIES):
                try:
                    art_data = await logos_client.get(
                        f"/api/app/books/{quote(resource_id)}/articles/{quote(article_id)}"
                    )
                    break
                except Exception as e:
                    if attempt < MAX_RETRIES - 1:
                        delay = RETRY_BASE_DELAY * (2 ** attempt)
                        log(f"Retry {attempt + 1}/{MAX_RETRIES} for article {article_id} "
                            f"after {delay}s: {e}")
                        await asyncio.sleep(delay)
                    else:
                        log(f"Failed to fetch article {article_id} after "
                            f"{MAX_RETRIES} attempts: {e}")
                        failed_articles.append(article_id)
            fetch_elapsed += time.monotonic() - fetch_t0

        if art_data is None:
            # Strategy 1: exact TOC match (article ID is in the TOC list)
            recovered = _next_toc_article(toc_article_ids, article_id)
            if recovered:
                log(f"Chain broken at {article_id}, recovering via TOC → {recovered}")
                article_id = recovered
                continue
            # Strategy 2: trim last ID component to find a parent article
            recovered = await _recover_by_parent(resource_id, article_id)
            if recovered:
                log(f"Chain broken at {article_id}, recovered via parent → {recovered}")
                article_id = recovered
                continue
            # Strategy 3: scan forward alphabetically from the failed ID
            recovered = await _recover_by_alpha_scan(resource_id, article_id, visited)
            if recovered:
                log(f"Chain broken at {article_id}, recovered via alpha scan → {recovered}")
                article_id = recovered
                continue
            log(f"Cannot continue walk: lost nextArticleId at {article_id} "
                f"and no recovery available")
            break

        process_t0 = time.monotonic()
        article = art_data.get("article", {})
        html_content = article.get("content", "")
        next_id = art_data.get("nextArticleId")

        if html_content:
            node = toc_index.lookup(html_content) if toc_index else None
            md, html_bible_refs, page_markers = html_to_markdown_with_refs(html_content)

            if md.strip():
                text_refs = extract_scripture_refs(md)
                all_refs = list(dict.fromkeys(html_bible_refs + text_refs))

                metadata = {
                    "resource_id": resource_id,
                    "resource_title": resource_title,
                    "article_id": article_id,
                    "title": node.title if node else None,
                    "author": node.author if node else None,
                    "heading_path": node.heading_path if node else None,
                    "scripture_refs": all_refs,
                }

                drafts = await chunker.chunk(md, metadata)
                _assign_pages(drafts, page_markers)

                if drafts:
                    # Assign batch key based on accumulated passage count
                    batch_key = f"b{batch_number:04d}"
                    serialized = [d.model_dump() for d in drafts]
                    # The drafts' offsets are relative to `md`; keep it so the
                    # batch can rebase them onto the document it becomes.
                    await save_article_text(resource_id, article_id, md)
                    await insert_chunks(resource_id, article_id, batch_key, serialized)
                    total_chunks_saved += len(drafts)
                    batch_chunk_count += len(drafts)

                    # Roll to next batch when threshold reached
                    if batch_chunk_count >= STORE_BATCH_SIZE:
                        sealed_key = batch_key
                        batch_number += 1
                        batch_chunk_count = 0
                        if batch_queue is not None:
                            await _enqueue_or_abort(
                                batch_queue, sealed_key, sibling,
                            )

                total_articles += 1

                if total_articles % 20 == 0:
                    avg_fetch_ms = fetch_elapsed * 1000 / max(total_articles, 1)
                    avg_proc_ms = process_elapsed * 1000 / max(total_articles, 1)
                    log(f"Walk progress: {total_articles} articles, "
                        f"{total_chunks_saved} chunks checkpointed "
                        f"(avg fetch {avg_fetch_ms:.0f}ms, "
                        f"process {avg_proc_ms:.0f}ms)")

        # Checkpoint progress every article
        await upsert_ingest_progress(
            resource_id=resource_id,
            title=resource_title,
            abbreviated=abbreviated,
            last_article_id=article_id,
            index=total_articles,
            total=total_articles,
            walk_complete=False,
            authors=sorted(unique_authors),
        )

        article_id = next_id
        process_elapsed += time.monotonic() - process_t0

    # If a partial batch is pending at end-of-walk, hand it off so the
    # storer can finish it. The wrapper function emits the None sentinel
    # afterwards regardless of how this function exits.
    if batch_queue is not None and batch_chunk_count > 0:
        partial_key = f"b{batch_number:04d}"
        await _enqueue_or_abort(batch_queue, partial_key, sibling)

    # Did the walk actually cover the book? Reaching the end of the
    # nextArticleId chain only proves the walk ran out of chain, not that it
    # started at the top: a walk beginning mid-book ends cleanly having missed
    # everything before it. The TOC is the independent record of what the book
    # contains, so it is what "complete" is measured against.
    #
    # Only when the TOC speaks the same ID space as the articles. Some books
    # index their TOC by byte offset ("1~47461"), and comparing those against
    # visited article IDs reports every entry missing on a walk that in fact
    # covered the whole book. No overlap at all means the TOC cannot answer
    # this question, not that the walk failed.
    toc_ids_are_article_ids = any(toc_id in visited for toc_id in toc_article_ids)
    missed_toc_articles = (
        [toc_id for toc_id in toc_article_ids if toc_id not in visited]
        if toc_ids_are_article_ids
        else []
    )
    if toc_article_ids and not toc_ids_are_article_ids:
        log(f"TOC entries are not article IDs for this book "
            f"(e.g. {toc_article_ids[0]}), so completeness is measured by "
            f"reaching the start and end of the article chain")
    if missed_toc_articles:
        log(f"Walk reached the end of the chain but {len(missed_toc_articles)} "
            f"of {len(toc_article_ids)} TOC articles were never visited "
            f"(first: {', '.join(missed_toc_articles[:5])})")

    # Mark walk complete
    walk_complete = (
        article_id is None
        or (max_articles > 0 and total_articles >= max_articles)
    ) and not missed_toc_articles
    await upsert_ingest_progress(
        resource_id=resource_id,
        title=resource_title,
        abbreviated=abbreviated,
        last_article_id=article_id or "",
        index=total_articles,
        total=total_articles,
        walk_complete=walk_complete,
        authors=sorted(unique_authors),
    )

    # Per-phase totals — useful for sizing the value of concurrent fetch (k=2).
    # If process << fetch, k=2 saves only the small process portion; the
    # ratio matters more than absolute numbers.
    fetch_ms_avg = (fetch_elapsed * 1000 / total_articles) if total_articles else 0
    process_ms_avg = (process_elapsed * 1000 / total_articles) if total_articles else 0
    log(f"Walk {'complete' if walk_complete else 'stopped'}: "
        f"{total_articles} articles, {total_chunks_saved} chunks checkpointed, "
        f"{len(failed_articles)} failures "
        f"(fetch {fetch_elapsed:.1f}s avg {fetch_ms_avg:.0f}ms; "
        f"process {process_elapsed:.1f}s avg {process_ms_avg:.0f}ms)")

    return {
        "missed_toc_articles": missed_toc_articles,
        "resource_id": resource_id,
        "resource_title": resource_title,
        "toc_entries": toc_node_count,
        "authors": sorted(unique_authors),
        "walk_status": "complete" if walk_complete else "partial",
        "articles_walked": total_articles,
        "chunks_checkpointed": total_chunks_saved,
        "failed_articles": failed_articles,
        "resumed_from": resumed_from,
        "timing": {
            "fetch_seconds": round(fetch_elapsed, 2),
            "process_seconds": round(process_elapsed, 2),
            "fetch_ms_per_article": round(fetch_ms_avg, 1),
            "process_ms_per_article": round(process_ms_avg, 1),
        },
    }


# ── Phase 2: Store with Adaptive Retry ───────────────────────────────────────


#: Joins consecutive articles in a batch document. Two newlines so the boundary
#: reads as a paragraph break and no chunker later fuses two articles' prose.
ARTICLE_SEPARATOR = "\n\n"


async def _rebase_offsets(
    resource_id: str,
    batch_key: str,
    chunk_rows: list[dict],
    drafts: list[PassageDraft],
) -> str | None:
    """Shift per-article draft offsets onto the concatenated batch document.

    The chunker addresses one article; a stored document is a batch of many, so
    without this every article's chunks would claim to start at offset 0 of the
    document and quote verification would resolve to the wrong article.

    Returns the document's canonical text, or ``None`` when any article's text
    is missing — which happens only for chunks staged before this was recorded.
    In that case the offsets are left alone and no canonical text is stored, so
    the document is reported as unanchorable rather than silently wrong.
    """
    ordered_ids: list[str] = []
    for row in chunk_rows:
        article_id = row.get("article_id")
        if article_id and (not ordered_ids or ordered_ids[-1] != article_id):
            ordered_ids.append(article_id)

    texts = await get_article_texts(resource_id, ordered_ids)
    missing = [a for a in ordered_ids if a not in texts]
    if missing or not ordered_ids:
        log(
            f"Batch {batch_key}: no canonical text ({len(missing)} article(s) "
            f"staged before article text was recorded); passage offsets left "
            f"article-relative and the document will not be re-anchorable."
        )
        return None

    start_of: dict[str, int] = {}
    cursor = 0
    for article_id in ordered_ids:
        start_of[article_id] = cursor
        cursor += len(texts[article_id]) + len(ARTICLE_SEPARATOR)

    document_text = ARTICLE_SEPARATOR.join(texts[a] for a in ordered_ids)

    for row, draft in zip(chunk_rows, drafts, strict=True):
        shift = start_of.get(row.get("article_id"))
        if shift is None:
            continue
        draft.char_start += shift
        draft.char_end += shift

    return document_text


async def _store_with_retry(
    resource_id: str,
    batch_key: str,
    chunk_rows: list[dict],
    ingestion,
    doc_metadata: dict,
    resource_title: str,
    *,
    depth: int = 0,
) -> dict:
    """Attempt to store a batch of PassageDrafts. On failure, halve and retry."""
    def _parse_draft(raw):
        return json.loads(raw) if isinstance(raw, str) else raw

    drafts = [PassageDraft(**_parse_draft(row["draft_json"])) for row in chunk_rows]
    # Re-number positions sequentially within this batch so they're unique
    # per document (ingest_drafts creates one document per batch).
    for i, draft in enumerate(drafts):
        draft.position = i
    count = len(drafts)

    document_text = await _rebase_offsets(resource_id, batch_key, chunk_rows, drafts)

    batch_title = f"{resource_title} (batch {batch_key})"
    source = f"logos:{resource_id}:batch:{batch_key}"

    # Only the store itself is retryable. The bookkeeping that follows it used
    # to sit inside this `try`, so a failure *after* a successful store — the
    # response parse, or the checkpoint write — was read as "the store failed"
    # and the halves were written as fresh documents beside the parent that had
    # already landed. That is how "Blessed Are the Peacemakers" came to exist
    # twice: 7 parent batches and 14 halves, 1,377 duplicated passages, every
    # search against it returning each result two ways.
    try:
        result = await ingestion.ingest_drafts(
            title=batch_title,
            document_type="logos_book",
            passage_drafts=drafts,
            source=source,
            metadata=doc_metadata,
            full_text=document_text,
        )
    except EmbeddingUnavailable:
        # Halving answers "this batch was too big". It cannot answer "the
        # embedding backend is not there", and splitting 50 passages into
        # 6+6+6+7+... against a host that is switched off just makes the same
        # call sixteen times. The walk's checkpointed chunks survive, so the
        # storage phase resumes once embedding is back.
        log(f"Batch {batch_key}: embedding is unavailable — stopping rather "
            f"than halving. The checkpointed chunks are kept; re-run this "
            f"book once the embedding server is reachable.")
        raise
    except Exception as e:
        error_msg = str(e)
        log(f"Batch {batch_key}: failed to store {count} passages "
            f"(depth={depth}): {error_msg}")

        # An exception does not prove nothing was written. Ask the corpus
        # before duplicating into it: retrying a store that actually succeeded
        # is worse than not retrying at all, because the damage is silent and
        # only shows up as doubled search results months later.
        landed = await ingestion.find_existing(source=source)
        if landed:
            log(f"Batch {batch_key}: store reported failure but the document "
                f"is present; treating as stored rather than duplicating it")
            await _record_stored(resource_id, batch_key, landed[0].get("document_id"))
            return {"stored": count, "failed": 0, "batch_key": batch_key}

        # Can we halve?
        if count > MIN_BATCH_SIZE and depth < MAX_RETRY_DEPTH:
            mid = count // 2
            half_a = chunk_rows[:mid]
            half_b = chunk_rows[mid:]
            key_a = f"{batch_key}a"
            key_b = f"{batch_key}b"

            # Reassign chunk rows to new sub-batch keys
            await reassign_chunk_batch_keys(
                [r["id"] for r in half_a], key_a
            )
            await reassign_chunk_batch_keys(
                [r["id"] for r in half_b], key_b
            )
            # Update local rows to match
            for r in half_a:
                r["batch_key"] = key_a
            for r in half_b:
                r["batch_key"] = key_b

            log(f"Halving batch {batch_key} → {key_a} ({len(half_a)}), "
                f"{key_b} ({len(half_b)})")

            result_a = await _store_with_retry(
                resource_id, key_a, half_a, ingestion,
                doc_metadata, resource_title, depth=depth + 1,
            )
            result_b = await _store_with_retry(
                resource_id, key_b, half_b, ingestion,
                doc_metadata, resource_title, depth=depth + 1,
            )
            return {
                "stored": result_a["stored"] + result_b["stored"],
                "failed": result_a["failed"] + result_b["failed"],
                "batch_key": batch_key,
            }
        else:
            # Cannot halve further — mark as permanently failed
            await mark_chunks_failed(resource_id, batch_key, error_msg)
            log(f"Batch {batch_key}: permanently failed {count} passages")
            return {"stored": 0, "failed": count, "batch_key": batch_key}

    # Stored. Bookkeeping failures from here are logged and swallowed: the
    # passages are in the corpus, and re-running the batch to fix a checkpoint
    # row is what created duplicates in the first place.
    await _record_stored(resource_id, batch_key, result.get("document_id"))
    log(f"Batch {batch_key}: stored {count} passages "
        f"(document {result.get('document_id')})")
    return {"stored": count, "failed": 0, "batch_key": batch_key}


async def _record_stored(resource_id: str, batch_key: str, document_id) -> None:
    """Checkpoint a stored batch, never raising into the store path."""
    try:
        doc_id = UUID(document_id) if isinstance(document_id, str) else document_id
        await mark_chunks_stored(resource_id, batch_key, doc_id)
    except Exception as e:  # noqa: BLE001 - the passages are safely stored
        log(f"Batch {batch_key}: stored, but checkpointing it failed: {e}")


# Per-batch store timeout. One hung batch shouldn't take down the whole
# ingest. See PIPELINED_INGEST.md §3.D.
STORE_BATCH_TIMEOUT_SEC = 300


async def _consume_and_store(
    resource_id: str,
    ingestion,
    resource_title: str,
    batch_keys: AsyncIterable[str],
) -> dict:
    """Phase 2 core. Consume batch keys from any async iterable and store
    each batch to the corpus.

    doc_metadata is rebuilt per batch from logos_ingest_progress so that
    authors discovered mid-walk show up on later batches (§3.C). Each batch
    is wrapped in a wait_for so a hung embedder can't take down the run (§3.D).
    """
    total_stored = 0
    total_failed = 0
    batch_results: list[dict] = []

    async for batch_key in batch_keys:
        chunk_rows = await get_chunks_for_batch(resource_id, batch_key)
        if not chunk_rows:
            continue

        progress = await get_ingest_progress(resource_id)
        doc_metadata = {
            "resource_id": resource_id,
            "abbreviated_title": progress["abbreviated_title"] if progress else "",
            "authors": (
                list(progress["authors"])
                if progress and progress.get("authors")
                else []
            ),
        }

        try:
            result = await asyncio.wait_for(
                _store_with_retry(
                    resource_id, batch_key, chunk_rows, ingestion,
                    doc_metadata, resource_title,
                ),
                timeout=STORE_BATCH_TIMEOUT_SEC,
            )
        except TimeoutError:
            log(f"Batch {batch_key}: store timed out after "
                f"{STORE_BATCH_TIMEOUT_SEC}s; marking failed")
            failed_count = await mark_chunks_failed(
                resource_id, batch_key,
                f"store timeout ({STORE_BATCH_TIMEOUT_SEC}s)",
            )
            result = {
                "stored": 0,
                "failed": failed_count or len(chunk_rows),
                "batch_key": batch_key,
            }

        total_stored += result["stored"]
        total_failed += result["failed"]
        batch_results.append(result)

    log(f"Storage complete: {total_stored} stored, {total_failed} failed "
        f"across {len(batch_results)} batch(es)")

    return {
        "store_status": "complete",
        "total_stored": total_stored,
        "total_failed": total_failed,
        "batches_processed": len(batch_results),
        "batch_results": batch_results,
    }


async def _store_batches(resource_id: str, ingestion) -> dict:
    """Serial Phase 2 entry point — used by the mop-up retry loop and by
    callers without a queue. The pipelined handler invokes
    _consume_and_store directly with _aiter_queue.
    """
    progress = await get_ingest_progress(resource_id)
    if not progress:
        return {"store_status": "no_progress_found", "total_stored": 0, "total_failed": 0}

    keys = sorted(await get_pending_batch_keys(resource_id))
    if not keys:
        log(f"No pending batches for {resource_id}")
        return {"store_status": "nothing_pending", "total_stored": 0, "total_failed": 0}

    log(f"Storing {len(keys)} pending batch(es) for {resource_id}")
    return await _consume_and_store(
        resource_id, ingestion, progress["resource_title"], _aiter_list(keys),
    )


# ── Whole-book assembly ───────────────────────────────────────────────────────

#: Longest node title kept. Lexicon entries run their whole gloss onto the
#: first line, and a title is for recognising a place, not reading it.
_TITLE_LIMIT = 90

_MARKUP = re.compile(r"[*_`]+")
_WHITESPACE = re.compile(r"\s+")


def _entry_title(text: str) -> str | None:
    """The name an article should be cited by, taken from its own first line.

    The staged TOC metadata cannot supply this: its ``title`` is the *section*
    heading, and LSJ has thirty-seven of those across 188,724 articles — so
    every entry in the lexicon would be cited as "I. Authors and Works".

    The articles name themselves. A lexicon entry opens with its headword and
    then declines it ("ἀλληλοκτονέω, *slay each other*, Hp.Ep.17"), so the text
    up to the first comma is the headword and the rest is gloss. Prose sections
    open with their own heading and carry no such comma, and keep the line.
    """
    first = _WHITESPACE.sub(" ", text.lstrip().split("\n", 1)[0]).strip()
    first = _MARKUP.sub("", first).strip()
    if not first:
        return None

    # Comma in the Greek lexica ("ἀλληλοκτονέω, *slay each other*"), colon in
    # the Hebrew one ("זִיזָא: n.m."). Whichever comes first is the boundary.
    cut = min(
        (i for i in (first.find(","), first.find(":")) if i > 0),
        default=-1,
    )
    head = first[:cut].strip() if cut > 0 else ""
    # Only when the separator divides a headword from a gloss. A headword is a
    # word, sometimes an inflected pair; punctuation inside a sentence ("In the
    # following pages, which are...") is not a boundary, and cutting there
    # yields a fragment rather than a name. Word count tells them apart.
    if 1 < len(head) <= 60 and len(head) < len(first) and len(head.split()) <= 2:
        return head

    return first if len(first) <= _TITLE_LIMIT else first[:_TITLE_LIMIT].rstrip() + "…"


def _assemble_book(
    articles: list[tuple[str, str]],
) -> tuple[str, dict[str, tuple[int, int]]]:
    """Join a book's articles into one canonical text, keeping each one's span.

    The same concatenation rule the per-batch path used, applied to the whole
    book rather than a hundred articles at a time. Offsets are what make a
    passage quotable, so this is the single place the arithmetic lives.
    """
    parts: list[str] = []
    spans: dict[str, tuple[int, int]] = {}
    cursor = 0
    for article_id, text in articles:
        spans[article_id] = (cursor, cursor + len(text))
        parts.append(text)
        cursor += len(text) + len(ARTICLE_SEPARATOR)
    return ARTICLE_SEPARATOR.join(parts), spans


def _book_sections(
    articles: list[tuple[str, str]],
    spans: dict[str, tuple[int, int]],
    headings: dict[str, str | None],
) -> list[dict]:
    """A flat section list for `build_node_tree`: headings, then their articles.

    Consecutive articles under one TOC heading share a section node, so an
    outline can be read a level at a time instead of as a hundred thousand
    siblings.
    """
    sections: list[dict] = []
    current: str | None = None
    heading_index: int | None = None

    for article_id, text in articles:
        start, end = spans[article_id]
        heading = headings.get(article_id)
        if heading and heading != current:
            current = heading
            heading_index = len(sections)
            sections.append(
                {"char_start": start, "char_end": end, "level": 1, "heading": heading}
            )
        elif heading_index is not None:
            # `build_node_tree` widens parents to cover their children, but only
            # a heading that owns its articles reads correctly in an outline.
            sections[heading_index]["char_end"] = end

        sections.append(
            {
                "char_start": start,
                "char_end": end,
                "level": 2 if current else 1,
                "heading": _entry_title(text),
                "article_id": article_id,
            }
        )
    return sections


async def _store_resource(
    resource_id: str,
    ingestion,
    resource_title: str,
    doc_metadata: dict,
) -> dict:
    """Store a walked book as one document, with its structure.

    Storing per batch of a hundred chunks was never a chunking decision: it was
    the only shape `ingest_drafts` offered, since it creates one document per
    call and there was no way to add to an existing one. The corpus recorded the
    consequence — LSJ as 1,903 documents named "(batch b0000)" and up, 2,525
    documents for thirteen books, and no structure anywhere, because a batch
    boundary is an artefact of the walk and describes nothing about the book.

    The walk stays as it was, checkpointing every article; resumability lives in
    the staging tables, which is the right place for it. Only the store changes:
    once, at the end, with the whole text and the tree that addresses it.
    """
    chunks = await get_all_pending_chunks(resource_id)
    if not chunks:
        return {"store_status": "nothing_pending", "total_stored": 0, "total_failed": 0}

    articles = await get_ordered_article_texts(resource_id)
    if not articles:
        log(f"{resource_id}: chunks staged but no article text; cannot anchor them")
        return {"store_status": "no_article_text", "total_stored": 0,
                "total_failed": len(chunks)}

    document_text, spans = _assemble_book(articles)

    drafts: list[PassageDraft] = []
    headings: dict[str, str | None] = {}
    unanchored = 0
    for position, row in enumerate(chunks):
        raw = row["draft_json"]
        payload = json.loads(raw) if isinstance(raw, str) else raw
        article_id = row["article_id"]
        if article_id not in spans:
            unanchored += 1
            continue

        metadata = payload.get("metadata") or {}
        if article_id not in headings:
            path = metadata.get("heading_path")
            headings[article_id] = path[0] if isinstance(path, list) and path else None

        draft = PassageDraft(**payload)
        # Chunker offsets address one article; they must address the book.
        offset = spans[article_id][0]
        draft.char_start += offset
        draft.char_end += offset
        draft.position = position
        drafts.append(draft)

    if unanchored:
        log(f"{resource_id}: {unanchored} chunk(s) reference an article with no "
            f"stored text and were left out rather than anchored to the wrong place")

    sections = _book_sections(articles, spans, headings)
    node_drafts = build_node_tree(
        sections, text_length=len(document_text), title=resource_title
    )

    log(f"{resource_id}: storing {len(drafts)} passages, {len(node_drafts)} nodes, "
        f"{len(document_text):,} chars as one document")

    result = await ingestion.ingest_drafts(
        title=resource_title,
        document_type="logos_book",
        passage_drafts=drafts,
        source=f"logos:{resource_id}",
        metadata=doc_metadata,
        full_text=document_text,
        node_drafts=node_drafts,
    )

    document_id = result.get("document_id")
    if document_id:
        await mark_resource_chunks_stored(resource_id, UUID(str(document_id)))

    return {
        "store_status": "complete",
        "total_stored": len(drafts),
        "total_failed": unanchored,
        "document_id": document_id,
        "nodes": len(node_drafts),
        "articles": len(articles),
    }


async def rechunk_and_store_resource(
    resource_id: str,
    ingestion,
    resource_title: str,
    doc_metadata: dict,
) -> dict:
    """Rebuild a book from its staged article text, without walking Logos again.

    The walk is the expensive, fragile half — 188,724 articles for LSJ, over an
    API that times out. Its output is kept: `logos_ingest_article_texts` holds
    the exact markdown each article was chunked from. So a chunker version bump
    does not need the network, only the chunker.

    That matters more than convenience here. The staged *chunks* are a previous
    chunker's output: 251,768 of them against the 68,973 passages the corpus
    holds today, because 5.0 stopped treating a scripture index as six hundred
    boundaries. Replaying them would quietly undo that. Re-chunking the text
    they came from does not.
    """
    articles = await get_ordered_article_texts(resource_id)
    if not articles:
        return {"store_status": "no_article_text", "total_stored": 0, "total_failed": 0}

    staged = await get_article_metadata(resource_id)
    # Page markers live in the HTML, which re-chunking never sees. Recovering
    # them from the staged drafts is the difference between a corpus you can
    # cite and one you can only search.
    staged_pages = await get_article_page_markers(resource_id)
    chunker = VerseChunker()
    document_text, spans = _assemble_book(articles)

    drafts: list[PassageDraft] = []
    headings: dict[str, str | None] = {}
    for article_id, text in articles:
        recovered = staged.get(article_id, {})
        path = recovered.get("heading_path")
        headings[article_id] = path[0] if isinstance(path, list) and path else None

        metadata = {
            "resource_id": resource_id,
            "resource_title": resource_title,
            "article_id": article_id,
            "title": recovered.get("title"),
            "author": recovered.get("author"),
            "heading_path": path,
            "scripture_refs": recovered.get("scripture_refs") or [],
        }
        article_drafts = await chunker.chunk(text, metadata)
        # Before rebasing: markers are article-relative, as chunker output is.
        _assign_pages(article_drafts, staged_pages.get(article_id) or [])

        offset = spans[article_id][0]
        for draft in article_drafts:
            draft.char_start += offset
            draft.char_end += offset
            draft.position = len(drafts)
            drafts.append(draft)

    sections = _book_sections(articles, spans, headings)
    node_drafts = build_node_tree(
        sections, text_length=len(document_text), title=resource_title
    )

    log(f"{resource_id}: re-chunked {len(articles)} articles into {len(drafts)} "
        f"passages, {len(node_drafts)} nodes, {len(document_text):,} chars")

    result = await ingestion.ingest_drafts(
        title=resource_title,
        document_type="logos_book",
        passage_drafts=drafts,
        source=f"logos:{resource_id}",
        metadata=doc_metadata,
        full_text=document_text,
        node_drafts=node_drafts,
    )

    return {
        "store_status": "complete",
        "total_stored": len(drafts),
        "total_failed": 0,
        "document_id": result.get("document_id"),
        "nodes": len(node_drafts),
        "articles": len(articles),
    }


# ── Tool Handler ──────────────────────────────────────────────────────────────


async def _run_mop_up_retries(
    resource_id: str,
    ingestion,
    store_result: dict,
    max_retries: int = 3,
    sleep_seconds: float = 5.0,
) -> dict:
    """Retry transiently-failed chunks (e.g. GPU OOM). Each retry's
    batch_results entries are tagged with the attempt number so the merged
    list is unambiguous. Mutates and returns store_result.
    """
    for attempt in range(1, max_retries + 1):
        if store_result["total_failed"] == 0:
            break
        log(f"Retrying {store_result['total_failed']} failed passages "
            f"(attempt {attempt}/{max_retries})...")
        await asyncio.sleep(sleep_seconds)  # let GPU memory settle
        await reset_failed_chunks(resource_id)
        retry = await _store_batches(resource_id, ingestion)
        for r in retry.get("batch_results", []):
            r["attempt"] = attempt
        store_result["total_stored"] += retry.get("total_stored", 0)
        store_result["total_failed"] = retry.get("total_failed", 0)
        store_result["batch_results"].extend(retry.get("batch_results", []))
    return store_result


def _normalize_results(walk_result, store_result) -> tuple[dict, dict]:
    """Coerce gather() exception payloads into stable dict shapes.

    Walker's try/finally guarantees the sentinel was emitted before any
    exception escaped, so the storer can drain normally even when the walker
    raises (§3.A). This function only translates raised exceptions into the
    same dict shape the success path returns, so the handler's merge
    (`{**walk_result, **store_result}`) is uniform.
    """
    if isinstance(walk_result, BaseException):
        log(f"Walker raised: {walk_result!r}")
        walk_result = {
            "walk_status": "errored",
            "error": str(walk_result),
        }
    if isinstance(store_result, BaseException):
        log(f"Storer raised: {store_result!r}")
        store_result = {
            "store_status": "errored",
            "error": str(store_result),
            "total_stored": 0,
            "total_failed": 0,
            "batches_processed": 0,
            "batch_results": [],
        }
    return walk_result, store_result


@tool(
    id="logos.ingest_book",
    description="Ingest a Logos book via the articles API. Accepts either a "
    "resource_id (e.g. 'LLS:BLSSDRPCMKRTHLF') or a logos.com product URL "
    "(e.g. 'https://www.logos.com/product/248390/...'). Two-phase pipeline: "
    "(1) walk the article chain and checkpoint chunks to the plugin DB, "
    "(2) store to corpus with adaptive retry on GPU OOM. "
    "Resumable — safe to re-run after crashes.",
    input_schema={
        "type": "object",
        "properties": {
            "resource_id": {
                "type": "string",
                "description": "The Logos resource ID to ingest "
                "(e.g. 'LLS:BLSSDRPCMKRTHLF'). Provide this OR url.",
            },
            "url": {
                "type": "string",
                "description": "A logos.com product page URL. The resourceId "
                "is extracted from the page. Provide this OR resource_id.",
            },
            "max_articles": {
                "type": "integer",
                "description": "Maximum articles to ingest. Defaults to 0 (all).",
                "default": 0,
            },
        },
    },
)
async def handler(
    resource_id: str | None = None,
    url: str | None = None,
    max_articles: int = 0,
    ingestion=None,
    **kwargs,
) -> dict:
    if not resource_id and not url:
        raise ValueError("Provide either resource_id or url")
    if not resource_id:
        resource_id = await _resolve_url_to_resource_id(url)
        log(f"Resolved {url} → {resource_id}")

    await run_migrations()

    book_data = await _fetch_book_data(resource_id)

    if ingestion is None:
        # No-storer mode: just walk. Preserves previous behavior for callers
        # that want a checkpoint without committing to corpus.
        walk_stats = await _walk_and_checkpoint(
            resource_id, max_articles, book_data=book_data,
        )
        return {
            **walk_stats,
            "store_status": "skipped",
            "warning": "No ingestion client — walk completed but nothing stored",
        }

    resource_title = book_data.get("resourceTitle", resource_id)

    # Walk to the end, then store the book once. The walker and storer used to
    # run concurrently so that passages appeared while the walk was still
    # going, which is worth something on a book that takes hours. It cost the
    # document: a store every hundred chunks meant a document every hundred
    # chunks, because `ingest_drafts` creates one per call. Structure and
    # citation both address a document, so both were unavailable for the
    # entire corpus. Nothing is searchable until the walk finishes now, and the
    # staged chunks survive a crash exactly as before.
    walk_result = await _walk_and_checkpoint(resource_id, max_articles, book_data)

    progress = await get_ingest_progress(resource_id)
    doc_metadata = {
        "resource_id": resource_id,
        "abbreviated_title": progress["abbreviated_title"] if progress else "",
        "authors": (
            list(progress["authors"]) if progress and progress.get("authors") else []
        ),
    }

    try:
        store_result = await _store_resource(
            resource_id, ingestion, resource_title, doc_metadata,
        )
    except EmbeddingUnavailable:
        # The chunks are checkpointed; re-running the tool resumes at the store.
        log(f"{resource_id}: embedding is unavailable — the walk is checkpointed, "
            f"re-run once the embedding server is reachable.")
        store_result = {
            "store_status": "embedding_unavailable",
            "total_stored": 0,
            "total_failed": 0,
        }

    return {**walk_result, **store_result}
