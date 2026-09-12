"""logos.get_entry — verbatim lexicon-entry retrieval (read-only).

Returns the full byte-verbatim text of a dictionary/lexicon entry from the
user's Logos library so future survey work can quote entries instead of
summarizing them.

Resolution strategy (all reads, no writes):

1. ``/api/app/books/{resource_id}`` for the resource title and chain shape,
   plus ``.../tableofcontents`` for letter-section offset ranges. The TOC
   returns top-level nodes only and letter sections 404 as direct articles,
   so entries are located by seeking the ``nextArticleId`` chain, not by
   dereferencing TOC ids.
2. ``/api/app/guides/word?reference={headword}`` (word study) for
   ``lemmaLinkInformation`` — per-resource ``indexedOffset``/``indexedLength``
   pairs plus glosses. This is what disambiguates an unpointed headword such
   as ``שער`` (resolves to Aramaic ``שְׂעַר`` "hair") and what reports empty
   sections for Strong's-number references.
3. Chain seeking: numeric chains (``PREFIX.N.M``, e.g. BDB ``LBDB.2184.4``)
   support offset bisection over ``N``; alphabetic chains (e.g. CHALOT
   ``X.50``) are entered via a probed letter-article map. Recovery inside a
   walk mirrors the read path of ``logos.tools.ingest_book``
   (``_next_toc_article`` / ``_recover_by_alpha_scan``) but performs no
   checkpointing, no DB access, and no corpus writes.
4. Resolution is bounded by ``timeout_s`` (default 55s): word-study link
   seeks run first, then the section scan, and on expiry the tool returns
   partial candidates with ``scan_complete: false`` instead of a dead call.

On multiple candidates the tool returns the candidate list with
distinguishing glosses and lets the caller pick — it never silently takes
the first hit. A caller that has picked can fetch the entry directly with
``article_id``.
"""

from __future__ import annotations

import asyncio
import html as _html
import json
import re
import sys
import unicodedata
from urllib.parse import quote

import httpx
from research_engine.plugins.sdk import tool

from logos.http.client import logos_client
from logos.lib.logger import log

#: Hard cap on characters of entry text per response. BDB article HTML runs
#: ~86KB for a ~5K indexed-char entry (markup-heavy spans), HALOT entries run
#: to ~17KB, so the cap sits above every entry observed while keeping MCP
#: responses sane. Overflow sets ``truncated`` with ``continuation`` fields.
MAX_TEXT_CHARS = 100_000

#: Global guard on articles fetched per call (seeks + scans combined).
MAX_WALK_ARTICLES = 5_000

#: Default resolution deadline in seconds. Most MCP hosts time out near 60s,
#: so the default leaves margin for response serialization. ``0``/negative
#: disables the deadline (batch survey work).
_DEFAULT_TIMEOUT_S = 55

#: Scan-range end when the book root omits ``resourceLength``: walk to chain
#: end instead of collapsing the last section to a 1-char range. Walks
#: already terminate at ``nextArticleId`` exhaustion, so the sentinel is
#: bounded by the book, not the budget.
_SCAN_TO_END = sys.maxsize

#: Parallel chunk-walkers for numeric-scheme section scans. The API
#: tolerates wide fan-out (a 33-way probe burst is fine), so section scans
#: chunk aggressively; each walker is still a polite sequential chain walk.
_WALK_CONCURRENCY = 8

#: Target indexed-char span per parallel scan chunk.
_CHUNK_TARGET_BYTES = 100_000

#: Forward probes when a numeric chain id 404s during a seek (gap vs. end).
_GAP_PROBE_LIMIT = 8

#: Far probes (past an 8-miss) distinguishing a mid-chain gap from
#: end-of-book during the exponential phase. Two extra fetches at most.
_GAP_FAR_PROBES = (64, 512)

#: Single-letter article probes for alphabetic chains (CHALOT uses arbitrary
#: codes such as X=צ, V=שׁ, plus an ARAM supplement — discovered, not assumed).
_ALPHA_PROBE_IDS = [chr(c) for c in range(ord("A"), ord("Z") + 1)] + ["ARAM"]

_NUMERIC_CHAIN_RE = re.compile(r"^(.+)\.(\d+)\.(\d+)$")

_HEADWORD_TAG_RE = re.compile(r"<span\b[^>]*>", re.IGNORECASE)
_REL_HEADWORD_RE = re.compile(r"""rel\s*=\s*["']headword["']""", re.IGNORECASE)
_DATA_HEADWORD_RE = re.compile(r"""data-headword\s*=\s*"([^"]*)\"""")
_DATA_LANG_RE = re.compile(r"""data-headword-language\s*=\s*"([^"]*)\"""")

_TAG_RE = re.compile(r"<[^>]+>")

#: Hebrew points/cantillation + shin/sin dots, ZWJ/ZWNJ, directional marks.
_STRIP_RE = re.compile("[\u0591-\u05c7\u200d\u200e\u200f\u2060]")

_TIER_RANK = {"exact": 0, "normalized": 1, "consonantal": 2, "offset": 3}


class _Halt(Exception):
    """Internal: stop resolution and return partial candidates.

    Raised when the deadline expires or the fetch budget runs out. Caught
    only in ``_resolve_candidates``; never escapes to the caller. Every
    ``except Exception`` on the resolution path must re-raise it.
    """


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and asyncio.get_running_loop().time() >= deadline:
        raise _Halt


async def _gather_cancel(coros: list) -> list:
    """``asyncio.gather`` that cancels siblings when one fails.

    Plain ``gather`` leaves siblings running detached after the first
    exception — under a deadline those stragglers burn fetches (and flake
    hang-tests). Here every sibling is cancelled before re-raising.
    """
    tasks = [asyncio.create_task(c) for c in coros]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for t in tasks:
            if not t.done():
                t.cancel()
        raise


# ── Text helpers ──────────────────────────────────────────────────────────────


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def _consonantal(s: str) -> str:
    """Strip diacritics and format marks for consonantal comparison.

    Unpointed שער must match both pointed שַׁעַר (gate) and שְׂעַר (hair);
    Greek headwords likewise match across accentuation.
    """
    no_marks = _STRIP_RE.sub("", unicodedata.normalize("NFD", s))
    return "".join(ch for ch in no_marks if unicodedata.category(ch) != "Mn")


def _first_consonant(s: str) -> str:
    for ch in _consonantal(s):
        if unicodedata.category(ch).startswith("L"):
            return ch
    return ""


def _match_tier(span_text: str, headword: str) -> str | None:
    if span_text == headword:
        return "exact"
    if _nfc(span_text) == _nfc(headword):
        return "normalized"
    span_c, head_c = _consonantal(span_text), _consonantal(headword)
    if span_c and span_c == head_c:
        return "consonantal"
    return None


def _iter_headword_spans(content: str) -> list[tuple[str, str]]:
    """Yield (headword, language) pairs from ``rel="headword"`` spans."""
    spans: list[tuple[str, str]] = []
    for m in _HEADWORD_TAG_RE.finditer(content):
        tag = m.group(0)
        if not _REL_HEADWORD_RE.search(tag):
            continue
        hw = _DATA_HEADWORD_RE.search(tag)
        if not hw:
            continue
        lang = _DATA_LANG_RE.search(tag)
        spans.append((hw.group(1), lang.group(1) if lang else ""))
    return spans


def _gloss_snippet(content: str, limit: int = 160) -> str:
    """Short distinguishing text for a candidate (metadata, not the entry).

    Whitespace collapsing is fine here — the verbatim rule applies to the
    returned entry ``text``, never to derived glosses.
    """
    text = _TAG_RE.sub(" ", content)
    text = _html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[:limit].rstrip() + "…"
    return text


# ── Chain-recovery read path (mirrors logos.tools.ingest_book) ───────────────
#
# Copied — not imported — deliberately: importing ingest_book would drag in
# the ingest writers (logos.db.queries, chunker, corpus storer) and break
# this module's read-only hard rule. Only the read path (plain GETs, no
# checkpointing) is reproduced here.


def _next_toc_article(toc_ids: list[str], failed_id: str) -> str | None:
    """Find the next article in the TOC after *failed_id*."""
    try:
        idx = toc_ids.index(failed_id)
    except ValueError:
        return None
    if idx + 1 < len(toc_ids):
        return toc_ids[idx + 1]
    return None


async def _recover_by_alpha_scan(
    resource_id: str,
    failed_id: str,
    visited: set[str] | None = None,
    *,
    cache: dict[str, dict],
    budget: list[int],
    deadline: float | None,
) -> str | None:
    """Scan forward alphabetically from a failed article's prefix.

    Builds candidates by advancing from the failed article's stem, trying
    both the stem with incremented last character and shortened two-letter
    prefixes. Returns the first article that exists.
    """
    dot = failed_id.find(".")
    if dot < 0:
        return None
    prefix = failed_id[: dot + 1]

    rest = failed_id[dot + 1 :]
    second_dot = rest.find(".")
    stem = rest[:second_dot] if second_dot >= 0 else rest
    if not stem:
        return None

    candidates: list[str] = []

    if len(stem) >= 1:
        fl = ord(stem[0].upper())
        for c in range(fl + 1, ord("Z") + 1):
            candidates.append(f"{prefix}{chr(c)}")

    for length in range(2, len(stem)):
        trunc = stem[:length].upper()
        last = ord(trunc[-1])
        base = trunc[:-1]
        for c in range(last + 1, ord("Z") + 1):
            candidates.append(f"{prefix}{base}{chr(c)}")

    if len(stem) >= 3:
        stem_base = stem[:-1].upper()
        last_char = ord(stem[-1].upper())
        for c in range(last_char + 1, ord("Z") + 1):
            candidates.append(f"{prefix}{stem_base}{chr(c)}")

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
            art_data = await _fetch_deadline(
                resource_id,
                candidate,
                cache=cache,
                budget=budget,
                deadline=deadline,
                retries=0,
            )
        except _Halt:
            raise
        except Exception:
            continue
        if art_data is None:
            continue
        log(
            f"Alpha scan: {candidate} exists → "
            f"nextArticleId={art_data.get('nextArticleId')}"
        )
        return candidate
    return None


async def _recover_numeric(
    resource_id: str,
    prefix: str,
    n: int,
    visited: set[str],
    *,
    cache: dict[str, dict],
    budget: list[int],
    deadline: float | None,
) -> str | None:
    """Gap-tolerant forward step for numeric chains: first existing N.M."""
    for step in range(_GAP_PROBE_LIMIT):
        candidate = f"{prefix}.{n + step}.0"
        if candidate in visited:
            continue
        try:
            env = await _fetch_deadline(
                resource_id,
                candidate,
                cache=cache,
                budget=budget,
                deadline=deadline,
                retries=0,
            )
        except _Halt:
            raise
        except Exception:
            continue
        if env is not None:
            return candidate
    return None


# ── Fetch plumbing ────────────────────────────────────────────────────────────


def _is_404(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        resp = exc.response
        return resp is not None and resp.status_code == 404
    return False


async def _fetch_article(
    resource_id: str,
    article_id: str,
    *,
    retries: int = 1,
) -> dict | None:
    """Fetch one article envelope; None when it does not exist (404).

    Non-404 failures are retried once, then propagate — a broken walk
    should surface, not silently stop.
    """
    path = f"/api/app/books/{quote(resource_id)}/articles/{quote(article_id)}"
    for attempt in range(retries + 1):
        try:
            data = await logos_client.get(path)
            return data if isinstance(data, dict) else None
        except Exception as exc:
            if _is_404(exc):
                return None
            if attempt >= retries:
                raise
            await asyncio.sleep(1.0)
    return None  # unreachable


async def _fetch_deadline(
    resource_id: str,
    article_id: str,
    *,
    cache: dict[str, dict],
    budget: list[int],
    deadline: float | None,
    retries: int = 1,
) -> dict | None:
    """Fetch one article through the shared cache, budget, and deadline.

    Cache hits cost nothing. Misses decrement *budget* and raise ``_Halt``
    when the budget is spent or the deadline passes (including mid-fetch
    via ``wait_for``) — the resolver catches it and returns partials.
    A 404 is an ordinary miss: ``None``, no raise.
    """
    hit = cache.get(article_id)
    if hit is not None:
        return hit
    _check_deadline(deadline)
    if budget[0] <= 0:
        raise _Halt
    budget[0] -= 1
    if deadline is None:
        env = await _fetch_article(resource_id, article_id, retries=retries)
    else:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise _Halt
        try:
            env = await asyncio.wait_for(
                _fetch_article(resource_id, article_id, retries=retries),
                remaining,
            )
        except TimeoutError:
            raise _Halt from None
    if env is not None:
        cache[article_id] = env
    return env


def _article_bounds(envelope: dict) -> tuple[int, int]:
    art = envelope.get("article", {}) if isinstance(envelope, dict) else {}
    try:
        off = int(art.get("indexedOffset", 0) or 0)
    except (TypeError, ValueError):
        off = 0
    try:
        length = int(art.get("indexedLength", 0) or 0)
    except (TypeError, ValueError):
        length = 0
    return off, length


def _article_content(envelope: dict) -> str:
    art = envelope.get("article", {}) if isinstance(envelope, dict) else {}
    content = art.get("content", "")
    return content if isinstance(content, str) else ""


# ── Chain seeking ─────────────────────────────────────────────────────────────


def _parse_chain_id(article_id: str) -> tuple[str, int, int] | None:
    """Parse ``PREFIX.N.M`` numeric chain ids; None for alphabetic ones."""
    m = _NUMERIC_CHAIN_RE.match(article_id)
    if not m:
        return None
    return m.group(1), int(m.group(2)), int(m.group(3))


async def _seek_numeric(
    resource_id: str,
    prefix: str,
    target: int,
    cache: dict[str, dict],
    budget: list[int],
    deadline: float | None = None,
) -> tuple[str, dict] | None:
    """Find the article whose ``[offset, offset+length)`` contains *target*.

    Returns ``(article_id, envelope)``. Exponential upper bound,
    gap-tolerant bisection over the ``N`` component, then a short
    ``nextArticleId`` walk. Fetched envelopes are memoized in *cache*;
    *budget*[0] counts fetches against MAX_WALK_ARTICLES. Raises ``_Halt``
    when the budget is spent or *deadline* passes.
    """

    async def at(n: int) -> tuple[str, dict] | None:
        aid = f"{prefix}.{n}.0"
        env = await _fetch_deadline(
            resource_id, aid, cache=cache, budget=budget, deadline=deadline
        )
        return (aid, env) if env is not None else None

    async def probe_up(n: int) -> tuple[str, dict] | None:
        """First existing article at N.0, N+1.0, … (gap tolerance)."""
        for step in range(_GAP_PROBE_LIMIT):
            hit = await at(n + step)
            if hit is not None:
                return hit
        return None

    def contains(env: dict) -> bool:
        off, length = _article_bounds(env)
        return off <= target < off + length

    def cached_bracket() -> tuple[
        tuple[int, str, dict] | None, tuple[int, str, dict] | None
    ]:
        below = None
        above = None
        for aid, env in cache.items():
            off, _ = _article_bounds(env)
            parsed = _parse_chain_id(aid)
            if parsed is None or parsed[0] != prefix:
                continue
            if off <= target and (below is None or off > below[0]):
                below = (off, aid, env)
            elif off > target and (above is None or off < above[0]):
                above = (off, aid, env)
        return below, above

    # Seed from shared cache when a sibling chunk already mapped this area.
    lo_id: str | None = None
    lo_env: dict | None = None
    lo = 0
    hi: int | None = None
    below, above = cached_bracket()
    if below is not None:
        _, lo_id, lo_env = below
        lo = int(lo_id.rsplit(".", 2)[1])
        if lo_env is not None and contains(lo_env):
            return lo_id, lo_env
    if above is not None:
        hi = int(above[1].rsplit(".", 2)[1])

    # Exponential upper bound from the top of the book (skipped when the
    # cache already brackets the target — see above).
    if lo_id is None:
        first = await probe_up(1)
        if first is None:
            return None
        lo_id, lo_env = first
        lo = int(lo_id.rsplit(".", 2)[1])
        if contains(lo_env):
            return lo_id, lo_env
    if lo_id is None or lo_env is None:
        raise RuntimeError("numeric seek lost its lower bound")
    n = lo + 1
    while budget[0] > 0 and hi is None:
        _check_deadline(deadline)
        hit = await probe_up(n)
        if hit is None:
            # 8 consecutive 404s: mid-chain gap or end-of-book? Single
            # far probes tell — a hit flows through the same bound
            # logic. Sweeping here would cost 16 fetches at every true
            # end-of-book; a false end merely degrades to the linear
            # walk below, never a miss.
            for far in _GAP_FAR_PROBES:
                hit = await at(n + far)
                if hit is not None:
                    break
            if hit is None:
                hi = n
                break
        found_id, env = hit
        found_n = int(found_id.rsplit(".", 2)[1])
        off, length = _article_bounds(env)
        if off <= target < off + length:
            return found_id, env
        if off > target:
            hi = found_n
            break
        lo, lo_id, lo_env = found_n, found_id, env
        n = max(found_n * 2, found_n + 1)
        if n > 1_000_000:
            hi = n
            break
    if hi is None:
        return None

    # Gap-tolerant bisection between lo and hi.
    best_id, best_env = lo_id, lo_env
    while lo + 1 < hi and budget[0] > 0:
        _check_deadline(deadline)
        mid = (lo + hi) // 2
        hit = await probe_up(mid)
        if hit is None:
            hi = mid
            continue
        found_id, env = hit
        found_n = int(found_id.rsplit(".", 2)[1])
        off, length = _article_bounds(env)
        if off <= target < off + length:
            return found_id, env
        if off <= target:
            lo, best_id, best_env = found_n, found_id, env
        else:
            hi = mid

    # Short forward walk from the best article at-or-below target.
    current_id, current = best_id, best_env
    seen: set[str] = set()
    while current is not None and budget[0] > 0:
        _check_deadline(deadline)
        if contains(current):
            return current_id, current
        off, _ = _article_bounds(current)
        if off > target:
            return None
        nxt = current.get("nextArticleId")
        if not nxt or nxt in seen:
            return None
        seen.add(nxt)
        current = await _fetch_deadline(
            resource_id, nxt, cache=cache, budget=budget, deadline=deadline
        )
        if current is None:
            return None
        current_id = nxt
    return None


_alpha_section_cache: dict[str, dict[int, str]] = {}


async def _alpha_offset_map(
    resource_id: str,
    cache: dict[str, dict],
    budget: list[int],
    deadline: float | None = None,
) -> dict[int, str]:
    """Probe candidate letter-article ids; map indexedOffset → article id.

    Probes count against the budget and honor the deadline; hits are
    stored in the shared *cache* so the ensuing walk doesn't re-fetch
    its entry point. The offset→id map itself is memoized per resource
    (in-memory read memoization, no writes).
    """
    if resource_id in _alpha_section_cache:
        return _alpha_section_cache[resource_id]

    async def probe(aid: str) -> tuple[int, str] | None:
        try:
            env = await _fetch_deadline(
                resource_id,
                aid,
                cache=cache,
                budget=budget,
                deadline=deadline,
                retries=0,
            )
        except _Halt:
            raise
        except Exception:
            return None
        if env is None:
            return None
        off, _ = _article_bounds(env)
        if off <= 0:
            return None
        return off, aid

    mapping: dict[int, str] = {}
    for found in await _gather_cancel([probe(a) for a in _ALPHA_PROBE_IDS]):
        if found is not None:
            mapping.setdefault(found[0], found[1])
    _alpha_section_cache[resource_id] = mapping
    return mapping


async def _seek_alpha(
    resource_id: str,
    target: int,
    cache: dict[str, dict],
    budget: list[int],
    deadline: float | None = None,
) -> tuple[str, dict] | None:
    """Find the article containing *target* on an alphabetic chain."""
    mapping = await _alpha_offset_map(resource_id, cache, budget, deadline)
    starts = sorted(mapping)
    if not starts:
        return None
    # Nearest probed article at or below target, then walk forward.
    idx = 0
    for i, off in enumerate(starts):
        if off <= target:
            idx = i
        else:
            break
    current_id: str | None = mapping[starts[idx]]
    seen: set[str] = set()
    while current_id and budget[0] > 0:
        _check_deadline(deadline)
        if current_id in seen:
            break
        seen.add(current_id)
        current = await _fetch_deadline(
            resource_id,
            current_id,
            cache=cache,
            budget=budget,
            deadline=deadline,
        )
        if current is None:
            recovered = await _recover_by_alpha_scan(
                resource_id,
                current_id,
                seen,
                cache=cache,
                budget=budget,
                deadline=deadline,
            )
            if not recovered:
                return None
            current_id = recovered
            continue
        off, length = _article_bounds(current)
        if off <= target < off + length:
            return current_id, current
        if off > target:
            return None
        current_id = current.get("nextArticleId")
    return None


async def _seek_offset(
    resource_id: str,
    chain_hint: str | None,
    prefix: str | None,
    target: int,
    cache: dict[str, dict],
    budget: list[int],
    deadline: float | None = None,
) -> tuple[str, dict] | None:
    """Seek the article containing *target*, whatever the chain scheme."""
    if prefix is not None:
        found = await _seek_numeric(
            resource_id, prefix, target, cache, budget, deadline
        )
        if found is not None:
            return found
    found = await _seek_alpha(resource_id, target, cache, budget, deadline)
    if found is not None or chain_hint is None:
        return found
    # Last resort: walk forward from the book root's article.
    current_id: str | None = chain_hint
    seen: set[str] = set()
    while current_id and budget[0] > 0:
        _check_deadline(deadline)
        if current_id in seen:
            return None
        seen.add(current_id)
        current = await _fetch_deadline(
            resource_id,
            current_id,
            cache=cache,
            budget=budget,
            deadline=deadline,
        )
        if current is None:
            return None
        off, length = _article_bounds(current)
        if off <= target < off + length:
            return current_id, current
        if off > target:
            return None
        current_id = current.get("nextArticleId")
    return None


# ── Section scanning ──────────────────────────────────────────────────────────


async def _walk_range_numeric(
    resource_id: str,
    prefix: str,
    start_id: str,
    end_offset: int,
    headword: str,
    language: str | None,
    cache: dict[str, dict],
    budget: list[int],
    out: list[tuple[str, dict, str, str, str]],
    deadline: float | None = None,
) -> None:
    """Walk one offset-bounded chunk, collecting headword matches.

    Appends ``(article_id, envelope, tier, matched_span, span_language)``
    to *out*.
    """
    current_id: str | None = start_id
    seen: set[str] = set()
    while current_id and budget[0] > 0:
        _check_deadline(deadline)
        if current_id in seen:
            break
        seen.add(current_id)
        env = await _fetch_deadline(
            resource_id,
            current_id,
            cache=cache,
            budget=budget,
            deadline=deadline,
        )
        if env is None:
            parsed = _parse_chain_id(current_id)
            n = parsed[1] + 1 if parsed else 0
            recovered = await _recover_numeric(
                resource_id,
                prefix,
                n,
                seen,
                cache=cache,
                budget=budget,
                deadline=deadline,
            )
            if not recovered:
                break
            current_id = recovered
            continue
        off, _ = _article_bounds(env)
        if off >= end_offset:
            break
        for span_text, span_lang in _iter_headword_spans(_article_content(env)):
            if language and span_lang.lower() != language.lower():
                continue
            tier = _match_tier(span_text, headword)
            if tier is not None:
                out.append((current_id, env, tier, span_text, span_lang))
                break
        current_id = env.get("nextArticleId")


async def _scan_section_numeric(
    resource_id: str,
    prefix: str,
    start_offset: int,
    end_offset: int,
    headword: str,
    language: str | None,
    cache: dict[str, dict],
    budget: list[int],
    deadline: float | None = None,
) -> list[tuple[str, dict, str, str, str]]:
    """Scan one letter section, chunking wide ranges across parallel walkers."""
    if end_offset >= _SCAN_TO_END:
        # Unknown section length (no resourceLength): one seed + walk to
        # chain end. Chunking an unbounded span would fire doomed seeks
        # at astronomical offsets.
        n_chunks = 1
    else:
        span = max(end_offset - start_offset, 1)
        n_chunks = min(_WALK_CONCURRENCY, max(1, span // _CHUNK_TARGET_BYTES + 1))
    out: list[tuple[str, dict, str, str, str]] = []
    if n_chunks == 1:
        seed = await _seek_numeric(
            resource_id,
            prefix,
            start_offset,
            cache,
            budget,
            deadline,
        )
        if seed is None:
            return out
        seed_id, _ = seed
        await _walk_range_numeric(
            resource_id,
            prefix,
            seed_id,
            end_offset,
            headword,
            language,
            cache,
            budget,
            out,
            deadline,
        )
        return [m for m in out if _article_bounds(m[1])[0] >= start_offset]

    # Bisect each chunk start, then walk chunks concurrently.
    sem = asyncio.Semaphore(_WALK_CONCURRENCY)

    async def one_chunk(
        chunk_start: int,
        chunk_end: int,
    ) -> list[tuple[str, dict, str, str, str]]:
        async with sem:
            local: list[tuple[str, dict, str, str, str]] = []
            seed = await _seek_numeric(
                resource_id,
                prefix,
                chunk_start,
                cache,
                budget,
                deadline,
            )
            if seed is None:
                return local
            seed_id, _ = seed
            await _walk_range_numeric(
                resource_id,
                prefix,
                seed_id,
                chunk_end,
                headword,
                language,
                cache,
                budget,
                local,
                deadline,
            )
            return [m for m in local if _article_bounds(m[1])[0] >= chunk_start]

    span = max(end_offset - start_offset, 1)
    bounds = [start_offset + (span * i) // n_chunks for i in range(n_chunks + 1)]
    jobs = [
        one_chunk(bounds[i], bounds[i + 1] if i + 1 < n_chunks else end_offset)
        for i in range(n_chunks)
    ]
    for found in await _gather_cancel(jobs):
        out.extend(found)
    return out


async def _scan_section_alpha(
    resource_id: str,
    start_id: str | None,
    chain_hint: str | None,
    end_offset: int,
    headword: str,
    language: str | None,
    cache: dict[str, dict],
    budget: list[int],
    deadline: float | None = None,
) -> list[tuple[str, dict, str, str, str]]:
    """Sequential section walk for alphabetic chains."""
    out: list[tuple[str, dict, str, str, str]] = []
    current_id = start_id or chain_hint
    seen: set[str] = set()
    while current_id and budget[0] > 0:
        _check_deadline(deadline)
        if current_id in seen:
            break
        seen.add(current_id)
        env = await _fetch_deadline(
            resource_id,
            current_id,
            cache=cache,
            budget=budget,
            deadline=deadline,
        )
        if env is None:
            recovered = await _recover_by_alpha_scan(
                resource_id,
                current_id,
                seen,
                cache=cache,
                budget=budget,
                deadline=deadline,
            )
            if not recovered:
                break
            current_id = recovered
            continue
        off, _ = _article_bounds(env)
        if off >= end_offset and off > 0:
            break
        for span_text, span_lang in _iter_headword_spans(_article_content(env)):
            if language and span_lang.lower() != language.lower():
                continue
            tier = _match_tier(span_text, headword)
            if tier is not None:
                out.append((current_id, env, tier, span_text, span_lang))
                break
        current_id = env.get("nextArticleId")
    return out


# ── Word-study lemma links ────────────────────────────────────────────────────


async def _lemma_links(
    headword: str,
    resource_id: str,
) -> tuple[list[dict], str | None]:
    """Per-resource ``(offset, length, gloss)`` links plus the guide's lemma.

    Returns ``([], None)`` when word study has nothing (e.g. Strong's-number
    references, which yield empty sections) — the caller falls back to the
    section scan rather than failing.
    """
    try:
        data = await logos_client.get(
            f"/api/app/guides/word?reference={quote(headword)}"
        )
    except Exception as exc:
        log(f"get_entry: word study unavailable for {headword!r}: {exc}")
        return [], None
    if not isinstance(data, dict):
        return [], None
    guide_ref = (data.get("guideInfo") or {}).get("reference")
    links: list[dict] = []
    for section in data.get("sections", []) or []:
        if not isinstance(section, dict) or section.get("kind") != "definition":
            continue
        details = ((section.get("content") or {}).get("definition") or {}).get(
            "lemmaDetails", {}
        ) or {}
        info = details.get("lemmaLinkInformation") or {}
        for link in info.get("lemmaLinks", []) or []:
            if not isinstance(link, dict):
                continue
            rid = str(link.get("resourceId", ""))
            if rid != resource_id and rid.lower() != resource_id.lower():
                continue
            try:
                off = int(link.get("indexedOffset"))
                length = int(link.get("indexedLength"))
            except (TypeError, ValueError):
                continue
            links.append(
                {
                    "indexed_offset": off,
                    "indexed_length": length,
                    "gloss": str(link.get("gloss", "") or ""),
                    "title": str(link.get("title", "") or ""),
                }
            )
    return links, guide_ref if isinstance(guide_ref, str) else None


# ── Output builders ───────────────────────────────────────────────────────────


def _entry_response(
    resource_title: str,
    article_id: str,
    envelope: dict,
    headword_label: str,
    start: int,
    match: str,
    scan_complete: bool,
) -> dict:
    content = _article_content(envelope)
    off, length = _article_bounds(envelope)
    start = max(int(start or 0), 0)
    window = content[start : start + MAX_TEXT_CHARS]
    truncated = len(content) > start + MAX_TEXT_CHARS
    return {
        "status": "entry",
        "resource_title": resource_title,
        "article_id": article_id,
        "headword": headword_label,
        "match": match,
        "indexed_offset": off,
        "indexed_length": length,
        "text": window,
        "truncated": truncated,
        "continuation": (
            {"article_id": article_id, "next_start": start + MAX_TEXT_CHARS}
            if truncated
            else None
        ),
        "scan_complete": scan_complete,
    }


def _candidate_row(
    article_id: str,
    envelope: dict,
    span_text: str,
    tier: str,
    gloss: str,
    *,
    span_lang: str = "",
    fallback_label: str = "",
) -> dict:
    off, length = _article_bounds(envelope)
    content = _article_content(envelope)
    return {
        "article_id": article_id,
        "headword": span_text or fallback_label,
        "language": span_lang,
        "gloss": gloss or _gloss_snippet(content),
        "indexed_offset": off,
        "indexed_length": length,
        "match": tier,
    }


# ── TOC sections ──────────────────────────────────────────────────────────────


def _toc_sections(toc_data: object) -> list[dict]:
    """Top-level TOC nodes as ``{title, offset}`` sorted by offset."""
    items: object = toc_data
    if isinstance(toc_data, dict):
        for key in ("items", "children", "toc", "tableOfContents"):
            if isinstance(toc_data.get(key), list):
                items = toc_data[key]
                break
    if not isinstance(items, list):
        return []
    sections: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            off = int(item.get("indexedOffset", 0) or 0)
        except (TypeError, ValueError):
            continue
        title = item.get("title", "")
        sections.append(
            {"title": title if isinstance(title, str) else "", "offset": off}
        )
    sections.sort(key=lambda s: s["offset"])
    return sections


def _is_letter_section(title: str) -> bool:
    stripped = _consonantal(title).strip()
    return 0 < len(stripped) <= 2


def _plan_scan_ranges(
    sections: list[dict],
    headword: str,
    guide_ref: str | None,
    link_offsets: list[int],
    resource_length: int,
) -> list[tuple[int, int]]:
    """Offset ranges that must be walked for *headword*.

    The letter section(s) whose first consonant matches the headword's, plus
    any trailing supplement sections (Biblical Aramaic, …) when word study
    resolved to a non-Hebrew lemma or a lemma link points outside the letter
    sections. Each range end is the next section's start (or resource end).
    """
    if not sections:
        return []
    first = _first_consonant(headword)
    ends = [s["offset"] for s in sections[1:]] + [resource_length or 0]
    letter_idx = [i for i, s in enumerate(sections) if _is_letter_section(s["title"])]
    last_letter = letter_idx[-1] if letter_idx else len(sections) - 1

    wanted: list[int] = []
    for i, s in enumerate(sections):
        if not _is_letter_section(s["title"]):
            continue
        if _first_consonant(s["title"]) == first and first:
            wanted.append(i)
    # Supplement sections (after the last letter section): include when the
    # lemma is not Hebrew proper or a link points beyond the lettered part.
    supplement = [i for i in range(last_letter + 1, len(sections))]
    is_aramaic = bool(guide_ref and ".arc." in guide_ref)
    letter_end = ends[last_letter] if letter_idx else 0
    link_outside = any(
        (off < sections[0]["offset"] if sections else False)
        or (letter_end and off >= letter_end)
        for off in link_offsets
    )
    # A headword whose first consonant matches no letter section (e.g. a
    # Strong's number) scans nothing: falling back to the supplements would
    # walk hundreds of unrelated articles for an empty result. Supplements
    # join only for Aramaic lemmas or out-of-section links.
    if supplement and (is_aramaic or link_outside):
        wanted.extend(supplement)

    ranges: list[tuple[int, int]] = []
    for i in sorted(set(wanted)):
        end = ends[i] or resource_length or _SCAN_TO_END
        if end <= sections[i]["offset"]:
            # Malformed TOC (next start precedes this start): don't invert.
            end = sections[i]["offset"] + 1
        ranges.append((sections[i]["offset"], end))
    return ranges


# ── Resolver ──────────────────────────────────────────────────────────────────


async def _resolve_candidates(
    resource_id: str,
    headword: str,
    language: str | None,
    chain_hint: str | None,
    prefix: str | None,
    sections: list[dict],
    resource_length: int,
    deadline: float | None = None,
) -> tuple[list[dict], bool, dict]:
    """Collect every matching article.

    Returns ``(candidates, scan_complete, cache)``. Link seeks run before
    the section scan so a deadline expiry degrades to "word-study answer
    + partial scan" rather than nothing. ``_Halt`` anywhere inside —
    deadline or spent budget — yields partials with ``False``.
    """
    cache: dict[str, dict] = {}
    budget = [MAX_WALK_ARTICLES]
    links, guide_ref = await _lemma_links(headword, resource_id)
    guide_lemma = guide_ref.rsplit(".", 1)[-1] if guide_ref else ""

    by_id: dict[str, dict] = {}
    complete = True
    try:
        # Path 1 — word-study lemma links: exact entry positions. The endorsed
        # article for this headword, even when it sits outside the lettered
        # sections (e.g. an Aramaic-supplement entry).
        for link in links:
            if budget[0] <= 0:
                complete = False
                break
            _check_deadline(deadline)
            hit = await _seek_offset(
                resource_id,
                chain_hint,
                prefix,
                link["indexed_offset"],
                cache,
                budget,
                deadline,
            )
            if hit is None:
                continue
            aid, env = hit
            content = _article_content(env)
            best_tier: str | None = None
            best_span = ""
            best_lang = ""
            has_lang_span = False
            for span_text, span_lang in _iter_headword_spans(content):
                if language:
                    if span_lang.lower() != language.lower():
                        continue
                    has_lang_span = True
                tier = _match_tier(span_text, headword)
                if tier is not None and (
                    best_tier is None or _TIER_RANK[tier] < _TIER_RANK[best_tier]
                ):
                    best_tier, best_span, best_lang = tier, span_text, span_lang
            if best_tier is None:
                if language and not has_lang_span:
                    # The filter names a language this article doesn't
                    # even carry: drop, don't emit a bogus offset hit.
                    continue
                best_tier, best_span, best_lang = "offset", "", ""
            row = _candidate_row(
                aid,
                env,
                best_span,
                best_tier,
                link["gloss"],
                span_lang=best_lang,
                fallback_label=guide_lemma or headword,
            )
            prev = by_id.get(aid)
            if prev is None or _TIER_RANK[best_tier] < _TIER_RANK[prev["match"]]:
                by_id[aid] = row

        # Path 2 — letter-section walk: the only way to catch homonyms and
        # entries word study did not link (I שַׁעַר vs II שַׁעַר; CHALOT entries).
        ranges = _plan_scan_ranges(
            sections,
            headword,
            guide_ref,
            [link["indexed_offset"] for link in links],
            resource_length,
        )
        link_gloss = {link["indexed_offset"]: link["gloss"] for link in links}
        for start_offset, end_offset in ranges:
            if budget[0] <= 0:
                complete = False
                break
            _check_deadline(deadline)
            if prefix is not None:
                matches = await _scan_section_numeric(
                    resource_id,
                    prefix,
                    start_offset,
                    end_offset,
                    headword,
                    language,
                    cache,
                    budget,
                    deadline,
                )
            else:
                mapping = await _alpha_offset_map(resource_id, cache, budget, deadline)
                at_or_below = [o for o in mapping if o <= start_offset]
                start_id = mapping[max(at_or_below)] if at_or_below else None
                matches = await _scan_section_alpha(
                    resource_id,
                    start_id,
                    chain_hint,
                    end_offset,
                    headword,
                    language,
                    cache,
                    budget,
                    deadline,
                )
            for aid, env, tier, span_text, span_lang in matches:
                off, _ = _article_bounds(env)
                gloss = link_gloss.get(off, "")
                row = _candidate_row(
                    aid,
                    env,
                    span_text,
                    tier,
                    gloss,
                    span_lang=span_lang,
                    fallback_label=headword,
                )
                prev = by_id.get(aid)
                if prev is None or _TIER_RANK[tier] < _TIER_RANK[prev["match"]]:
                    by_id[aid] = row
    except _Halt:
        complete = False

    candidates = sorted(by_id.values(), key=lambda r: r["indexed_offset"])
    return candidates, complete and budget[0] > 0, cache


@tool(
    id="logos.get_entry",
    description=(
        "Get the full verbatim text of a dictionary/lexicon entry from the "
        "user's Logos library (e.g. BDB, HALOT, CHALOT) by headword. "
        "Returns the entry text exactly as the API serves it. On multiple "
        "matches returns candidates with glosses for the caller to pick; "
        "pass article_id to fetch a picked candidate directly. "
        "Resolution is bounded by timeout_s (default 55s): slow lookups "
        "return partial candidates with scan_complete=false, never a "
        "dead call."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "resource_id": {
                "type": "string",
                "description": "Logos resource ID (e.g. LLS:46.30.16 for BDB).",
            },
            "headword": {
                "type": "string",
                "description": "Hebrew/Aramaic/Greek headword to look up.",
            },
            "language": {
                "type": "string",
                "description": (
                    "Optional headword-language filter over "
                    "data-headword-language spans (he, arc observed; "
                    "unknown codes match nothing)."
                ),
            },
            "article_id": {
                "type": "string",
                "description": "Fetch one article directly (from a candidates list).",
            },
            "start": {
                "type": "integer",
                "description": (
                    "Character offset into the entry HTML text (continuation paging)."
                ),
            },
            "timeout_s": {
                "type": "number",
                "description": (
                    "Resolution deadline in seconds (default 55). On "
                    "expiry returns partial candidates with "
                    "scan_complete=false. 0 or negative = unbounded "
                    "(batch survey work)."
                ),
            },
        },
        "required": ["resource_id"],
    },
)
async def handler(
    resource_id: str,
    headword: str = "",
    language: str | None = None,
    article_id: str | None = None,
    start: int = 0,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    **kwargs,
) -> str:
    if not resource_id:
        raise ValueError("resource_id is required")
    if not article_id and not headword:
        raise ValueError("either headword or article_id is required")

    if article_id:
        # Direct fetch skips resolution — and the book/TOC round trips
        # when the article envelope already carries the resource title.
        env = await _fetch_article(resource_id, article_id)
        if env is None:
            raise ValueError(f"Article {article_id!r} not found in {resource_id}")
        title = env.get("resourceTitle")
        if not isinstance(title, str) or not title:
            book = await logos_client.get(f"/api/app/books/{quote(resource_id)}")
            title = book.get("resourceTitle") if isinstance(book, dict) else None
            if not isinstance(title, str) or not title:
                title = resource_id
        spans = _iter_headword_spans(_article_content(env))
        label = spans[0][0] if spans else (headword or article_id)
        if headword:
            tiers = []
            for span_text, _ in spans:
                tier = _match_tier(span_text, headword)
                if tier is not None:
                    tiers.append(tier)
            match = min(tiers, key=_TIER_RANK.__getitem__) if tiers else "offset"
        else:
            # Caller addressed the article directly: exact by definition.
            match = "exact"
        return json.dumps(
            _entry_response(
                title,
                article_id,
                env,
                label,
                start,
                match,
                True,
            ),
            ensure_ascii=False,
            indent=2,
        )

    book = await logos_client.get(f"/api/app/books/{quote(resource_id)}")
    if not isinstance(book, dict):
        raise RuntimeError(f"Unexpected book response for {resource_id!r}")
    resource_title = str(book.get("resourceTitle") or resource_id)
    try:
        resource_length = int(book.get("resourceLength") or 0)
    except (TypeError, ValueError):
        resource_length = 0
    root_article = book.get("article") or {}
    chain_hint = root_article.get("articleId")
    if not isinstance(chain_hint, str):
        chain_hint = None
    parsed = _parse_chain_id(chain_hint) if chain_hint else None
    prefix = parsed[0] if parsed else None

    try:
        toc_data = await logos_client.get(
            f"/api/app/books/{quote(resource_id)}/tableofcontents"
        )
    except Exception as exc:
        log(f"get_entry: TOC unavailable for {resource_id}: {exc}")
        toc_data = []
    sections = _toc_sections(toc_data)

    if not headword:
        raise ValueError("either headword or article_id is required")
    try:
        timeout = float(timeout_s)
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT_S
    deadline = asyncio.get_running_loop().time() + timeout if timeout > 0 else None
    candidates, scan_complete, cache = await _resolve_candidates(
        resource_id,
        headword,
        language,
        chain_hint,
        prefix,
        sections,
        resource_length,
        deadline,
    )
    if not candidates:
        return json.dumps(
            {
                "status": "not_found",
                "resource_title": resource_title,
                "headword": headword,
                "found": False,
                "candidates": [],
                "scan_complete": scan_complete,
                "note": (
                    "No entry matched. Strong's-number references resolve "
                    "to empty word-study sections; otherwise check the "
                    "headword pointing or try language variants."
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    best_rank = min(_TIER_RANK[c["match"]] for c in candidates)
    best = [c for c in candidates if _TIER_RANK[c["match"]] == best_rank]
    if len(best) == 1:
        only = best[0]
        # The winner's envelope is already in the resolution cache; the
        # re-fetch below is a defensive fallback that never fires.
        env = cache.get(only["article_id"])
        if env is None:
            env = await _fetch_article(resource_id, only["article_id"])
        if env is None:  # pragma: no cover — vanished mid-call
            raise RuntimeError(
                f"Article {only['article_id']!r} vanished mid-resolution"
            )
        label = only["headword"] or headword
        return json.dumps(
            _entry_response(
                resource_title,
                only["article_id"],
                env,
                label,
                start,
                only["match"],
                scan_complete,
            ),
            ensure_ascii=False,
            indent=2,
        )
    return json.dumps(
        {
            "status": "ambiguous",
            "resource_title": resource_title,
            "headword": headword,
            "ambiguous": True,
            "candidates": best,
            "scan_complete": scan_complete,
            "note": (
                "Multiple entries matched. Pick an article_id and call "
                "logos.get_entry again with {resource_id, article_id}."
            ),
        },
        ensure_ascii=False,
        indent=2,
    )
