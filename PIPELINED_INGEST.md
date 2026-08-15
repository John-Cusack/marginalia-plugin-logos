# Pipelined Ingestion (Phase 1 ↔ Phase 2 Overlap)

**Status:** design (v3)
**Scope:** `logos.ingest_book` only.
**Risk:** low after the four blockers in §3 are addressed; without them, this design has correctness bugs that will bite in production.
**Expected gain:** `min(walk_time, store_time)` saved. Whether that's 5% or 45% depends on a measurement we haven't done yet — see §1.

---

## 1. Measure first (gate)

Before designing concurrency we should know which phase dominates. Add five lines of timing to the *current* handler and re-ingest a representative book:

```python
walk_t0 = time.monotonic()
walk_stats = await _walk_and_checkpoint(resource_id, max_articles)
walk_elapsed = time.monotonic() - walk_t0
store_t0 = time.monotonic()
store_stats = await _store_batches(resource_id, ingestion)
store_elapsed = time.monotonic() - store_t0
log(f"timing: walk={walk_elapsed:.1f}s store={store_elapsed:.1f}s")
```

Run on HALOT (`LLS:46.30.12`) — the longest book we have. Decide:

| Walk fraction of total | Recommendation |
|---|---|
| > 80% | **Skip this work.** Do option 2 (concurrent fetch / `k=2`) first; it attacks the actual bottleneck. |
| 40 – 80% | Do this work. Then layer option 2 on top. |
| < 40% | Embedding is dominant. Pipelining still helps, but consider Phase 2 batching/concurrency improvements first. |

The instrumentation is throwaway and costs nothing to add. Don't proceed to §2 without it.

---

## 2. Architecture

If we proceed: producer/consumer over an `asyncio.Queue` of batch-key strings. Walker enqueues a key when a batch fills (i.e. when `batch_chunk_count` rolls past `STORE_BATCH_SIZE`); storer reads keys and stores each batch. Chunk content lives in the DB throughout — the queue carries no payload.

```
   walker ──put(batch_key)──▶ asyncio.Queue ──get──▶ storer
     │                                                  │
     └─── insert_chunks ──▶ logos_ingest_chunks ◀── get_chunks_for_batch
```

Sentinel-based shutdown: walker puts `None` on exit (always — see §3.A); the queue→async-iterator wrapper translates that into iterator exhaustion so the storer doesn't have to know about sentinels.

Phases are isolated. Storer failures don't propagate up; affected chunks stay `failed` for the mop-up retry. Walker failures are captured in the handler.

---

## 3. Required foundations (mandatory blockers)

A code review of the v2 design surfaced four correctness bugs that this design — as written — would land. They are NOT optional follow-ups; they are mandatory for the merge. Each must be addressed in the implementation, with tests, before the pipelined path is enabled.

### 3.A Always-emit-sentinel (blocker #1)

**Bug:** `await asyncio.gather(walker_task, storer_task, return_exceptions=True)` deadlocks when the walker raises before its sentinel is enqueued. `gather` waits for both tasks; the storer is parked on `queue.get()`; nothing makes progress.

**Fix:** the walker emits the sentinel from a `try/finally` so it runs on every exit path (clean completion, exception, cancellation). The handler does NOT need a post-hoc "inject sentinel" step.

```python
async def _walk_and_checkpoint(resource_id, max_articles=0,
                               book_data=None, batch_queue=None, sibling=None):
    try:
        # ... walk and seal batches as before; on each seal:
        #     await _enqueue_or_abort(batch_queue, sealed_key, sibling)
        # ... at end of walk, if a partial batch remains:
        #     await _enqueue_or_abort(batch_queue, batch_key, sibling)
        ...
    finally:
        if batch_queue is not None:
            try:
                batch_queue.put_nowait(None)
            except asyncio.QueueFull:
                pass  # unbounded in our config; this guards future change
```

The `try/finally` placement is what makes `gather` correct. If you skip this, you have a silent-hang bug under any walker error.

### 3.B Canonical batch-state resolver (blockers #2 and #3)

**Bug A (#2):** `_resolve_batch_number` derived from `MAX(batch_key)` returns N when the last batch was sealed (full). Walker resumes writing into `bN` and grows it past `STORE_BATCH_SIZE`, producing oversized batches that risk OOM at store time.

**Bug B (#3):** `_resolve_batch_number` (no status filter) and `_active_batch_key` (status='pending' filter) can disagree when stored batches exist at higher numbers than pending ones. Walker resumes from one number; storer protects a different one. Storer either skips a real batch or processes the active one prematurely.

**Fix:** a single canonical resolver returning a tuple, used by both the walker (to know where to write) and the handler (to know which key to exclude from pre-load).

```python
import re

@dataclass(frozen=True)
class BatchState:
    next_batch_number: int   # the b{N:04d} the walker writes to next
    chunks_in_active: int    # how many chunks are already in that key (resume case)

async def _resolve_batch_state(resource_id: str) -> BatchState:
    """Single source of truth for resume + active-key protection.

    Returns the batch_number the walker should use and how many chunks already
    sit in it. Considers both pending and stored chunks so we never re-write
    into a sealed key.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT batch_key, COUNT(*) AS c, BOOL_OR(status = 'stored') AS any_stored
               FROM logos_ingest_chunks
               WHERE resource_id = $1
               GROUP BY batch_key
               ORDER BY batch_key DESC
               LIMIT 1""",
            resource_id,
        )
    if not row:
        return BatchState(next_batch_number=0, chunks_in_active=0)
    m = re.match(r"^b(\d+)", row["batch_key"])
    n = int(m.group(1)) if m else 0
    if row["any_stored"] or row["c"] >= STORE_BATCH_SIZE:
        return BatchState(next_batch_number=n + 1, chunks_in_active=0)
    return BatchState(next_batch_number=n, chunks_in_active=row["c"])

def active_batch_key(state: BatchState) -> str:
    return f"b{state.next_batch_number:04d}"
```

**Walker change:** on resume, set `batch_number = state.next_batch_number` and `batch_chunk_count = state.chunks_in_active`. (Prior versions of the walker kept `batch_chunk_count = 0` on resume, which over-grew batches; the new field fixes that too.)

**Handler change:** compute `state` once, derive `active_key = active_batch_key(state)`, exclude it from pre-load. Pass `state` (or just `book_data`) into the walker. **Do not query MAX twice with different filters.**

`_active_batch_key` from v2 is removed.

### 3.C Storer reads progress per batch (blocker #4)

**Bug:** v2 handler builds `doc_metadata` once, before launching tasks. On a fresh start `progress` is None, so `authors=[]`. The walker discovers authors from the TOC during the walk and writes them to `logos_ingest_progress`, but the storer is using the stale snapshot. Every batch on a fresh-book pipelined ingest gets `authors=[]` — silent metadata loss in the common case.

**Fix:** the storer re-reads progress per batch and rebuilds `doc_metadata`. One small DB read per batch; storer wall time is dominated by embedding so this is free.

```python
async def _consume_and_store(
    resource_id: str,
    ingestion,
    resource_title: str,
    batch_keys: AsyncIterable[str],
) -> dict:
    total_stored = 0
    total_failed = 0
    batch_results: list[dict] = []
    async for batch_key in batch_keys:
        chunk_rows = await get_chunks_for_batch(resource_id, batch_key)
        if not chunk_rows:
            continue
        # Re-read progress so authors discovered mid-walk make it into metadata.
        progress = await get_ingest_progress(resource_id)
        doc_metadata = {
            "resource_id": resource_id,
            "abbreviated_title": progress["abbreviated_title"] if progress else "",
            "authors": list(progress["authors"]) if progress and progress["authors"] else [],
        }
        try:
            result = await asyncio.wait_for(  # see §3.D
                _store_with_retry(
                    resource_id, batch_key, chunk_rows, ingestion,
                    doc_metadata, resource_title,
                ),
                timeout=300,
            )
        except TimeoutError:
            await mark_chunks_failed(resource_id, batch_key, "store timeout (300s)")
            result = {"stored": 0, "failed": len(chunk_rows), "batch_key": batch_key}
        total_stored += result["stored"]
        total_failed += result["failed"]
        batch_results.append(result)
    return {
        "store_status": "complete",
        "total_stored": total_stored,
        "total_failed": total_failed,
        "batches_processed": len(batch_results),
        "batch_results": batch_results,
    }
```

`doc_metadata` is no longer a handler parameter.

### 3.D Storer-side timeout (robustness — strongly recommended)

If `ingest_drafts` ever hangs (downstream embedder stuck), the storer hangs and the queue grows. Existing HTTP fetches in the walker have a 30s timeout; the storer side has none. Wrap `_store_with_retry` with `asyncio.wait_for(..., timeout=300)` (see §3.C). On timeout, mark the batch failed and let the mop-up loop retry it.

Not strictly a correctness bug, but losing the entire ingest to one stuck batch *is* a behavioral regression vs the current (serial) path. Land it with the blockers.

### 3.E Pre-merge checklist

Before the pipelined handler is enabled, every one of these must be true:

- [ ] Walker emits sentinel from `try/finally` (§3.A).
- [ ] `_resolve_batch_state` is the only place that derives batch position; walker and handler both call it (§3.B).
- [ ] Walker uses `state.chunks_in_active` to set `batch_chunk_count` on resume (§3.B).
- [ ] Storer re-reads progress and rebuilds `doc_metadata` per batch (§3.C).
- [ ] Storer wraps `_store_with_retry` with `asyncio.wait_for(timeout=300)` (§3.D).
- [ ] One unit test per blocker (see §9).

---

## 4. Design choices

| | Choice | Rationale |
|---|---|---|
| Coordination | `asyncio.Queue` (unbounded) | Memory cost is short strings; flow control isn't useful here (walker outrunning storer doesn't change wall time). |
| Shutdown | Walker emits `None` from `try/finally`; queue→async-iterator wrapper hides the sentinel from storer | Always-runs guarantee makes `gather` safe (§3.A). |
| Storage path | Single `_consume_and_store(...)` driven by an `AsyncIterable[str]` | One code path; pipelined and serial mop-up differ only in which iterable they pass. |
| Walker function | Existing `_walk_and_checkpoint` gains optional `book_data`, `batch_queue`, `sibling` parameters | No rename, no thin wrapper, no parallel function. |
| Metadata fetch | Pre-fetched once in handler, passed into walker; storer re-reads progress per batch for authors | Eliminates duplicate HTTP fetch *and* fixes the fresh-start author-loss bug. |
| Active-batch protection | One canonical resolver `_resolve_batch_state` (§3.B) | Walker's `batch_number` and handler's `active_key` are derived from the same value; impossible to disagree. |
| Storer-dead detection | `wait_for(queue.put, timeout=1.0)` + check sibling task | Real protection against deadlock when storer dies with full queue. (~1s notice latency; tunable.) |
| Storer hang protection | `asyncio.wait_for(_store_with_retry, timeout=300)` per batch | One hung batch can't take down the whole ingest. |
| Pre-existing pending batches | Sorted lex before enqueuing | Deterministic processing order; debugging-friendlier. |
| Failed-chunk retry | Unchanged: existing `_store_batches`-based mop-up after both tasks complete; each retry record gets an `attempt: N` field | OOM is transient; mop-up runs serially against a quiet GPU. Attempt tag avoids confusing duplicate-batch_key entries. |

---

## 5. File-by-file

```
logos/tools/ingest_book.py        EDIT
tests/unit/test_ingest_book.py    EDIT
PIPELINED_INGEST.md               this doc
```

No DB migration. No manifest change. No new dependencies.

---

## 6. Implementation sketch

The full diff goes through code review; below is the shape, with each blocker fix called out.

### 6.1 Helpers

```python
async def _fetch_book_data(resource_id: str) -> dict:
    return await logos_client.get(f"/api/app/books/{quote(resource_id)}")

async def _enqueue_or_abort(
    queue: asyncio.Queue, item, sibling: asyncio.Task,
) -> None:
    """Put on queue, but bail if the consumer task has died.
    1s polling latency is acceptable; promote to wait()-on-both if observed."""
    while True:
        try:
            await asyncio.wait_for(queue.put(item), timeout=1.0)
            return
        except TimeoutError:
            if sibling.done():
                raise RuntimeError("Storer terminated; walker aborting") from None

async def _aiter_queue(queue: asyncio.Queue) -> AsyncGenerator[str, None]:
    """Yield batch keys from queue until the None sentinel.
    task_done() in finally; runs even when the consumer raises (aclose) or
    when the sentinel arrives (no yield happens for None)."""
    while True:
        item = await queue.get()
        try:
            if item is None:
                return
            yield item
        finally:
            queue.task_done()

async def _aiter_list(items: Iterable[str]) -> AsyncGenerator[str, None]:
    for item in items:
        yield item
```

### 6.2 Canonical resolver (§3.B)

See the `_resolve_batch_state` and `BatchState` dataclass in §3.B. One regex (`r"^b(\d+)"`), one query, one source of truth.

### 6.3 Walker — modify in place, sentinel via `try/finally`

```python
async def _walk_and_checkpoint(
    resource_id: str,
    max_articles: int = 0,
    book_data: dict | None = None,             # NEW (informational; ignored on resume)
    batch_queue: asyncio.Queue | None = None,  # NEW
    sibling: asyncio.Task | None = None,       # NEW (only used with queue)
) -> dict:
    if book_data is None:
        book_data = await _fetch_book_data(resource_id)

    try:
        # On resume, use the canonical resolver — fixes blockers #2 + #3.
        state = await _resolve_batch_state(resource_id)
        batch_number = state.next_batch_number
        batch_chunk_count = state.chunks_in_active

        # ... existing walk logic with two integration points:

        # On batch seal:
        if batch_chunk_count >= STORE_BATCH_SIZE:
            sealed_key = batch_key
            batch_number += 1
            batch_chunk_count = 0
            if batch_queue is not None:
                await _enqueue_or_abort(batch_queue, sealed_key, sibling)

        # On loop exit (after final upsert_ingest_progress), enqueue any
        # partial batch:
        if batch_queue is not None and batch_chunk_count > 0:
            await _enqueue_or_abort(batch_queue, batch_key, sibling)

        return {...}  # walk_stats
    finally:
        # Blocker #1: sentinel must always fire so the storer can finish.
        if batch_queue is not None:
            try:
                batch_queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
```

The previous `batch_number = article_index // STORE_BATCH_SIZE` line is **deleted** — `_resolve_batch_state` replaces it. Ditto for `batch_chunk_count = 0` on resume.

### 6.4 Storage — see §3.C

`_consume_and_store` (single body) re-reads progress per batch and wraps `_store_with_retry` with a 300s timeout. `_store_batches` is preserved as a one-line shim used by mop-up:

```python
async def _store_batches(resource_id, ingestion) -> dict:
    progress = await get_ingest_progress(resource_id)
    if not progress:
        return {"store_status": "no_progress_found", "total_stored": 0, "total_failed": 0}
    keys = sorted(await get_pending_batch_keys(resource_id))
    return await _consume_and_store(
        resource_id, ingestion, progress["resource_title"], _aiter_list(keys),
    )
```

### 6.5 Handler

```python
async def handler(resource_id=None, url=None, max_articles=0, ingestion=None, **_):
    # ... existing url-resolve / migrations ...

    book_data = await _fetch_book_data(resource_id)
    resource_title = book_data.get("resourceTitle", resource_id)

    if ingestion is None:
        return {
            **(await _walk_and_checkpoint(resource_id, max_articles, book_data)),
            "store_status": "skipped",
            "warning": "No ingestion client — walk completed but nothing stored",
        }

    queue: asyncio.Queue = asyncio.Queue()

    # Pre-load resume batches, excluding the active one. Single canonical
    # resolver — blockers #2 + #3.
    state = await _resolve_batch_state(resource_id)
    active_key = active_batch_key(state)
    pending = sorted(await get_pending_batch_keys(resource_id))  # robustness #5
    for k in pending:
        if k != active_key:
            queue.put_nowait(k)

    storer_task = asyncio.create_task(
        _consume_and_store(
            resource_id, ingestion, resource_title, _aiter_queue(queue),
        ),
        name="logos-storer",
    )
    walker_task = asyncio.create_task(
        _walk_and_checkpoint(
            resource_id, max_articles, book_data, queue, storer_task,
        ),
        name="logos-walker",
    )

    walk_result, store_result = await asyncio.gather(
        walker_task, storer_task, return_exceptions=True,
    )
    walk_result, store_result = _normalize_results(walk_result, store_result)

    # Mop-up retries for transient OOM. Each attempt's batch_results is
    # tagged so the merged list is unambiguous (robustness #8).
    for attempt in range(1, 4):
        if store_result["total_failed"] == 0:
            break
        log(f"Retrying {store_result['total_failed']} failed passages "
            f"(attempt {attempt}/3)...")
        await asyncio.sleep(5)
        await reset_failed_chunks(resource_id)
        retry = await _store_batches(resource_id, ingestion)
        for r in retry.get("batch_results", []):
            r["attempt"] = attempt
        store_result["total_stored"] += retry["total_stored"]
        store_result["total_failed"] = retry["total_failed"]
        store_result["batch_results"].extend(retry.get("batch_results", []))

    return {**walk_result, **store_result}
```

`_normalize_results` only needs to coerce exceptions to dicts; the sentinel-injection branch from v2 is gone (blocker #1 closed at the source).

---

## 7. Cancellation

On `KeyboardInterrupt` / container shutdown, asyncio cancels both tasks. Specifically:

- The walker's `try/finally` still runs on `CancelledError`, so the sentinel is enqueued and the storer drains gracefully before its own cancellation propagates.
- The walker's most recent `insert_chunks` is one SQL statement; asyncpg either commits or rolls back atomically. No partial chunk row.
- In-flight `httpx` requests are cancelled cleanly by the `AsyncClient` context manager on task exit.
- The queue is garbage-collected. Pending batch keys are recoverable from `logos_ingest_chunks` with `status='pending'`.
- `logos_ingest_progress` reflects the last article successfully checkpointed.

Resume on next run picks up at `last_article_id` and re-enqueues whatever's still pending. **No special cleanup required.**

---

## 8. Failure modes

| Failure | Behavior |
|---|---|
| Walker raises (chain break, network) | `try/finally` enqueues sentinel ⇒ storer drains normally, `gather` returns (§3.A). |
| Storer raises (DB outage, unexpected) | `_enqueue_or_abort` notices `sibling.done()` and raises in walker. Both tasks settle; chunks remain in DB. |
| Single-batch store fails (OOM) | `_store_with_retry` halves the batch. `failed` chunks picked up by mop-up retry. Walker untouched. |
| Single-batch store hangs | `asyncio.wait_for(timeout=300)` aborts; chunks marked failed; mop-up retries (§3.D). |
| Process killed | All chunks at last DB-committed status. Resume continues. |
| Mop-up retries exhausted | `total_failed > 0` in result, with each attempt's failures tagged. Operator can re-run. |

---

## 9. Tests

`tests/unit/test_ingest_book.py` — additions, grouped by blocker:

**Blocker #1 (sentinel):**
1. `test_walker_emits_sentinel_on_clean_completion` — walk reaches end-of-chain → `None` is the last item on the queue.
2. `test_walker_emits_sentinel_on_exception` — walker raises mid-walk → `None` still arrives on the queue (the `try/finally` guarantee).
3. `test_walker_emits_sentinel_on_cancellation` — walker task cancelled → `None` still on queue, storer can drain.

**Blocker #2 + #3 (canonical resolver):**
4. `test_resolve_batch_state_empty_db` — no rows → `(0, 0)`.
5. `test_resolve_batch_state_partial_active_batch` — last key has 50 chunks, status='pending' → `(N, 50)`.
6. `test_resolve_batch_state_sealed_active_batch` — last key has 100 chunks → `(N+1, 0)`.
7. `test_resolve_batch_state_includes_stored` — last key (highest) is status='stored' → `(N+1, 0)`.
8. `test_resolve_batch_state_handles_halved_keys` — keys include `b0007a` and `b0007b` → returns 7 (or 8 if full); regex stops at first non-digit (polish #11).

**Blocker #4 (storer reads progress per batch):**
9. `test_storer_picks_up_authors_added_mid_walk` — first batch enqueued before walker writes authors to progress; second batch after. Assert second batch's `doc_metadata.authors` reflects the update; first does not (or both do, depending on race timing — at minimum, no batch ever has authors=[] when authors exist in progress at fetch time).

**Robustness:**
10. `test_storer_aborts_on_store_timeout` — `_store_with_retry` patched to hang; assert `wait_for` fires within ~1s of timeout, batch marked failed, storer continues to next batch.
11. `test_pre_load_pending_keys_sorted` — DB has b0003, b0001, b0002; queue receives them in lexical order.
12. `test_walker_aborts_when_storer_dies_with_full_queue` — storer task that raises immediately; walker tries to enqueue; expects `_enqueue_or_abort` to raise within ~2s rather than hanging.
13. `test_mop_up_retry_results_carry_attempt_field` — induce failures across two retry attempts; assert each `batch_results` entry has `attempt: 1` or `attempt: 2`.

**Behavior (kept lightweight):**
14. `test_consume_and_store_drains_iterable` — pass `_aiter_list(['b0000', 'b0001'])`; both processed; aggregate counts correct.

**Integration (real Postgres; gated):**
15. `test_pipelined_ingest_end_to_end_small_book` — actually run the handler against a fixture book with `max_articles=20`; assert all chunks stored, walk_complete, no orphan failed rows. *Marked `@pytest.mark.integration`; skipped when `RE_DB_URL` unset.*
16. `test_cancellation_leaves_chunks_recoverable` — start handler, cancel mid-walk via `task.cancel()`; assert pending chunks remain in DB and a re-run completes successfully. *Integration tier; cancellation + asyncpg interactions don't mock faithfully.*

The previous v2 doc's `test_pipelined_handler_overlaps_phases` is **removed**. Asserting "storer's first store happens before walker's last enqueue" is timing-sensitive and flaky; tests #1–#14 plus integration #15 cover the actual contract.

The 20 existing tests pass unchanged: `_walk_and_checkpoint`'s new params are all optional with current-behavior defaults.

---

## 10. Rollout

Strict ordering — each step lands and merges before the next begins.

1. **Land §1 timing instrumentation** in the existing handler. Re-ingest a real book. Decide go/no-go via §1 table. **Stop here if walk dominates.**
2. **Land the canonical resolver (§3.B)** independently: `_resolve_batch_state` + `BatchState` + tests #4–#8. Switch the walker's resume math to use it. This fixes the legacy resume bug *and* prepares the active-key derivation. Existing serial path benefits immediately (no oversized batches).
3. **Land §3.A walker `try/finally` sentinel emission** with tests #1–#3. The walker now accepts an optional `batch_queue` and emits the sentinel safely; nothing else changes.
4. **Land §3.C/§3.D storer refactor** (`_consume_and_store` reading progress per batch, `wait_for` timeout) with tests #9–#10. The serial path uses `_consume_and_store` underneath; behavior unchanged.
5. **Land the pipelined handler path** with tests #11–#14. Behind a hardcoded `True` (or env-var gate if you prefer a kill switch).
6. **Run integration tests #15–#16** against real Postgres + GPU.
7. **Re-ingest HALOT and a smaller book** to verify wall-time matches §1's prediction.
8. Optionally: **layer option 2 (`k=2`)** on top.

Steps 2–4 are independently shippable improvements even without the pipelined handler. Step 5 is the only change that introduces concurrency.

---

## 11. Out of scope

- Concurrent article fetching within Phase 1. Strictly orthogonal; composes cleanly.
- Cross-book pipelining. The handler is per-book.
- Streaming JSON parsing. Article responses are small.
- Refactoring the active-batch-key concept to support multi-writer walkers. Single-writer is invariant for v1.
- Tuning the `_enqueue_or_abort` poll interval to <1s. Acceptable as-is; revisit if observed.

---

## 12. What changed v1 → v2

(Retained from the v2 doc for history.)

- §1 measurement gate added.
- §3 resume bug called out and fixed up-front.
- One walker function with optional params.
- One storage path driven by `AsyncIterable`.
- Metadata pre-fetched once.
- Real storer-dead protection (`_enqueue_or_abort`).
- Unbounded queue.
- Cancellation paragraph + test.
- Honest `min(walk, store)` framing.

---

## 13. What changed v2 → v3

Surfaced in a code-review pass on v2; the four blockers below would have shipped silent correctness bugs.

**Blockers — must land first:**

- **§3.A always-emit sentinel.** Walker uses `try/finally` so `gather` cannot deadlock when the walker raises. v2's "inject sentinel after gather" was unreachable.
- **§3.B canonical batch-state resolver.** One function, one query, returning `(next_batch_number, chunks_in_active)`. Replaces both v2's incomplete `_resolve_batch_number` and the inconsistent `_active_batch_key`. Fixes the "last batch sealed at 100 → walker re-writes into it" edge case and the "stored vs pending filter mismatch" disagreement. Walker also uses `chunks_in_active` to set `batch_chunk_count` on resume — no more oversized-batch growth.
- **§3.C storer reads progress per batch.** v2's pre-built `doc_metadata` snapshot loses authors on every fresh-start ingest. Storer now re-reads progress per batch; `doc_metadata` is no longer a handler parameter.
- **§3.D `wait_for` timeout on `_store_with_retry`.** Not a v2 design bug, but the same risk class; one hung batch shouldn't take down the whole ingest.

**Robustness improvements:**

- Pre-load pending keys sorted lexically.
- Mop-up retry tags each `batch_results` entry with `attempt: N` to disambiguate duplicate batch_keys across retries.
- Tightened test list: dropped the flaky timing-overlap test; added blocker-specific tests; moved cancellation + end-to-end to integration tier with the real-DB gate.

**Polish:**

- `_resolve_batch_state` uses `re.match(r"^b(\d+)", key)` instead of `"".join(... isdigit())` (handles future suffix formats).
- Inline comment on `_aiter_queue`'s `task_done()` placement.
- `book_data` parameter on the walker noted as "informational; ignored on resume."

**Pre-merge checklist (§3.E):** five items, each with a corresponding test, that gate enabling the pipelined path.
