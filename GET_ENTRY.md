# logos.get_entry — Verbatim Lexicon-Entry Retrieval (v2 implementation doc)

**Status:** v2 implemented, unit-verified (26 tests green, ruff-clean) and live-accepted 2026-09-12 (4/4 live pins green, ~6min wall).
**Scope:** `logos/tools/get_entry.py`, unit + opt-in live tests. `pack.yaml` unchanged (input schema is code-declared).
**Risk:** v1's failure semantics are wrong for production use (no deadline; §4.A is a merge-blocker for any caller with a timeout). The resolution machinery itself is sound and stays.

**Review deltas vs the accepted draft** (all folded inline, listed here so the implementer sees them):
- §4.A rewritten: naive `asyncio.wait_for(_resolve_candidates(...))` **discards partials on cancel** — the prescribed fix could not return what it promised. v2 uses a cooperative deadline threaded through resolution (§4.A).
- §4.C extended: offset-tier candidates have empty `headword` today (verified live-shape: `{'headword': '', …}`) — candidates must fall back to link title/guide lemma. `_candidate_row` also reports the first span's language, not the matched span's — thread the matched language through.
- §4.F extended: `_recover_numeric`, `_recover_by_alpha_scan`, and `_alpha_offset_map` probes bypass the fetch budget entirely (signatures take neither budget nor cache — verified). Thread both.
- §4.H new: zero `resourceLength` collapses a wanted last section to a 1-char range (`(9000, 9001)` — verified). Scan-to-end sentinel instead.
- §2/§8: `_alpha_section_cache` is a module-global cross-call cache — contradicts "no cache across calls" as written. Kept and documented as read-only in-memory (no writes); tests already clear it.
- §5 corrected: "only the prefix format is taken from the book root" is false for `_seek_offset`'s last-resort fallback, which walks forward from the hint. Harmless (failure returns `None`, never wrong results) — documented, kept.
- §4.G corrected: schema already lists `el` as observed. Downgrade to observed-only (`he`, `arc`) until LSJ verifies it.
- §6 corrected: live tests **do** change (assert `status`/`match`/`scan_complete`; add CHALOT + ambiguity + timeout pins).
- §4.B: compat-key removal pinned to 0.2.0 (no in-repo callers — verified by grep; external MCP callers only).
- §4.E extended: `_seek_numeric`'s forward-walk guard is `budget[0] >= 0` (off-by-one — verified); make it `> 0`.

---

## 1. Problem and constraints

Future survey work must quote lexicon entries, not summarize them. The tool returns the full byte-verbatim text of one dictionary/lexicon entry from the user's Logos library. Hard constraints, in priority order:

1. **Read-only.** No imports from `logos.db`, `logos.ingest`, or `logos.tools.ingest_book`; no DB connections; no corpus rows. Enforced by tests (AST import scan + dry-run path trace), not by convention. The `_alpha_section_cache` is in-memory read memoization (letter-probe offsets keyed by resource id) — no writes, explicitly allowed, cleared between tests.
2. **Verbatim.** `text` is the article `content` exactly as served — no whitespace normalization, no tag stripping, no paraphrase. Derived metadata (glosses) may be normalized; entry text may not.
3. **Disambiguation is first-class.** Unpointed שער resolves via word study to Aramaic שְׂעַר "hair" while the user may mean gate שַׁעַר; homonyms are numbered (I שַׁעַר vs II שַׁעַר). Multiple candidates return a pick-list with glosses — never the first hit silently. Candidate `headword` is never empty (§4.C).
4. **Bounded responses.** Entries run to ~86KB of HTML (BDB) / ~17KB (HALOT). Overflow sets an explicit `truncated` flag with continuation fields; there is no silent truncation.

Non-goal: this tool does not ingest, index, or persist anything across calls. A persistent resolution cache is a future option (see §8), explicitly out of scope while the no-writes constraint stands.

---

## 2. Architecture (v1, as built; v2 keeps it)

One call is a pipeline of read stages sharing an article cache and a fetch budget:

```
book root ──┬──▶ resource_title, chain_hint, resourceLength
            │
TOC ────────┼──▶ letter-section offset ranges [start, next_start)
            │
word study ─┼──▶ lemmaLinks(resource): [(offset, length, gloss)] + guide lemma
            │
            ▼
path 1: seek each link offset ──▶ containing article (exact position)
path 2: walk each planned range ─▶ every headword-matching article (homonyms)
            │
            ▼
merge by article_id, best match-tier wins ──▶ 0 / 1 / N candidates
```

**Why two paths.** Word-study links give exact entry positions (and glosses), including entries outside the lettered sections — e.g. BDB שְׂעַר "hair" at offset 6041176 sits in the Biblical Aramaic supplement, past the ת section. But word study resolves to exactly one lemma, so homonyms the guide never mentions (I/II שַׁעַר) are invisible to path 1. Path 2 exists to catch them; path 1 exists so supplement entries are found even when path 2's ranges don't cover them. Neither subsumes the other.

**Chain schemes.** Two observed, auto-detected from the book root's `articleId`:

- *Numeric* (`PREFIX.N.M`, e.g. BDB `LBDB.2184.4`): offset-ordered in `N`. Seek = exponential upper bound → gap-tolerant bisection (8-probe 404 tolerance, §4.D) → short `nextArticleId` walk. Wide sections split into offset chunks walked concurrently (8 walkers); seeks share one cache so sibling chunks seed each other (`cached_bracket`).
- *Alphabetic* (e.g. CHALOT `X.50`): letter codes are arbitrary (`X`=צ, `V`=שׁ, `ARAM`=supplement — discovered, never assumed). Seek = concurrent A–Z+ARAM probe burst building an offset→id map, then a sequential `nextArticleId` walk. Sections are small here; no chunking. The probe map is memoized per resource id in `_alpha_section_cache` (in-memory, read-only; v2 counts its probes against the budget, §4.F).

**Recovery** mirrors the read path of `logos.tools.ingest_book` (`_next_toc_article`, `_recover_by_alpha_scan`, numeric gap-stepping) — copied, not imported, so the writer modules stay out of the import graph. TOC ids (`1~offset`) are never dereferenced as articles; a unit test asserts no fetched article id contains `~`.

**Match tiers** (best wins; ties across distinct articles → ambiguous): `exact` > `normalized` (NFC) > `consonantal` (diacritic-stripped — unpointed שער matches שַׁעַר and שְׂעַר) > `offset` (link position with no span match — word-study-endorsed but unverified).

---

## 3. Response contract (v2)

Three shapes, one discriminator. `status` is present on every response; `found: false` / `ambiguous: true` are kept until 0.2.0, then removed.

**Entry** (`status: "entry"`):

```json
{
  "status": "entry",
  "resource_title": "Enhanced Brown-Driver-Briggs Hebrew and English Lexicon",
  "article_id": "LBDB.2184.4",
  "headword": "מִשְׁפָּט",
  "match": "exact",
  "indexed_offset": 5710842,
  "indexed_length": 5265,
  "text": "<div class=\"resourcetext\">…",
  "truncated": false,
  "continuation": null,
  "scan_complete": true
}
```

- `match` is the winning tier (§2). `offset` here means "returned on word-study authority alone" — the caller decides whether to trust it.
- `scan_complete: false` means the fetch/time budget ran out before the homonym check finished (see §4.A). A single entry with `scan_complete: false` is provisional, not authoritative.
- `truncated: true` pairs with `continuation: {"article_id", "next_start"}`; resume by passing `next_start` as `start` (character offset — Python `str` units — into the article HTML). Cap: `MAX_TEXT_CHARS = 100_000`, set above the largest entry observed (BDB HTML ~86KB for a 5.3K indexed-char entry) so truncation is exceptional.

**Ambiguous** (`status: "ambiguous"`): `{status, resource_title, headword, candidates, scan_complete, note}` (+ `ambiguous: true` until 0.2.0) where each candidate is `{article_id, headword, language, gloss, indexed_offset, indexed_length, match}` sorted by offset. Only the best tier is listed. `headword` is never empty (falls back to link title, then the queried headword — §4.C); `language` is the matched span's language. Pick via `logos.get_entry({resource_id, article_id})`, which skips resolution and (after §4.F) skips book+TOC when the article envelope carries the title.

**Not found** (`status: "not_found"`): `{status, resource_title, headword, candidates: [], scan_complete, note}` (+ `found: false` until 0.2.0). Strong's-number references land here: word study returns empty sections and `H4941` matches no letter section, so nothing is scanned and the call costs 3 requests (~2.4s observed).

Input schema: `{resource_id (required), headword, language?, article_id?, start?, timeout_s?}`. Either `headword` or `article_id` is required. `language` filters `data-headword-language` spans (`he`, `arc` observed; `el` pending LSJ verification — §4.G). Unknown codes resolve to `not_found`, never match-all (§4.C). `timeout_s` bounds resolution: seconds, default 55, `0`/negative = unbounded (§4.A). `start`/`next_start` units are characters into the article HTML — stated in the schema.

---

## 4. v2 changes (implement all before production use)

### 4.A Deadline with partial results (blocker)

**Bug:** resolution is unbounded. Observed live: 63s (CHALOT צְדָקָה), 109s (BDB מִשְׁפָּט), 187s (BDB שער). Any caller with a timeout — most MCP hosts sit near 60s — gets nothing for exactly the calls where the tool is most valuable (ambiguous lookups need full section walks).

**Fix — cooperative deadline, not a wrapper.** A bare `asyncio.wait_for(_resolve_candidates(...))` cancels the task and **loses all partials** — it cannot "return what is known." Instead:

1. `handler` takes `timeout_s: float = 55`. When `> 0`, `deadline = loop.time() + timeout_s`, else `None`. Deadline covers resolution only (word-study links + seeks + scans); book/TOC/direct-`article_id` fetches stay unbounded (single requests each, httpx 120s backstop).
2. `_resolve_candidates(..., deadline: float | None)` threads the deadline into every fetch via a small `_fetch_deadline(resource_id, article_id, *, cache, budget, deadline)` helper: cache hits cost nothing; misses decrement the budget and raise an internal `_Halt` when the budget is spent or `wait_for(_fetch_article(...), remaining)` expires mid-fetch. All seek/walk/recovery/probe fetches on the resolution path go through it; every `except Exception` there re-raises `_Halt`.
3. On expiry the resolver stops immediately and returns `(sorted partials, False)`. Ordering guarantee: path 1 (links) runs before path 2 (scan) — already the case; keep it — so the timeout response degrades to "word-study answer + partial scan," never to nothing when word study answered.
4. The timeout path must not raise: partial candidates with `scan_complete: false` beat a dead call; the caller narrows via `article_id` or re-runs unbounded.
5. Hung-fetch backstop: a single hung TCP fetch resolves via `wait_for(remaining)` to expiry, not via the httpx 120s timeout — the handler returns within `timeout_s + slack` regardless of fetch behavior. `TimeoutError` from `wait_for` inside the resolver is expiry, never an error.

**Test:** §7.4 — fake with a hanging article endpoint (`asyncio.sleep` gated on an event, never set); fast link-seek endpoints; assert the handler returns within `timeout_s + slack` with `scan_complete: false` and the link-seek candidate present.

### 4.B Uniform status envelope

Add `status: "entry" | "ambiguous" | "not_found"` to all three shapes (§3). Keep `found: false` / `ambiguous: true` until 0.2.0 so existing external callers don't break (no in-repo callers — verified), then remove. Update the contract test to assert `status` on every path.

### 4.C `scan_complete` + `match` on entry responses; candidate-row honesty

- The single-hit branch currently drops both. After §4.A this is a correctness hole: a lone entry with an exhausted budget is provisional, and the caller can't see it. `_entry_response` gains `match` and `scan_complete` params (§3 schema). Surface `match: "offset"` honestly — it means the headword never matched a span.
- **Language-filter gap (same pass):** the `language` filter is bypassed on the link path — when no span matches the requested language, the code still emits the article as tier `offset`. When `language` is set, an offset-tier hit with no span in that language is dropped, not returned. Unknown language codes then resolve to `not_found`, never to match-all.
- **Empty-headword gap (same pass):** offset-tier rows carry `headword: ""` (verified). `_candidate_row` gains a `fallback_label` (guide lemma — last component of the word-study `reference` — then queried headword): `headword = span_text or fallback`. Candidates never ship blank headwords — add a contract assertion.
- **Wrong-language gap (same pass):** `_candidate_row` reports the article's *first* span language, not the matched span's. Thread the matched `(span_text, span_lang)` through; offset-tier rows (no span matched) report `""`.

### 4.D Gap-vs-end probing

`_GAP_PROBE_LIMIT = 8` treats 8 consecutive 404s as end-of-book during hi-search. The only wide 404 band observed (BDB past ~N.2500) really was the end — but a >8 mid-chain gap caps `hi` at the gap: bisection dies and the seek degrades to a linear `nextArticleId` walk, burning budget (measured on a 200-article fake with an 8-gap: 98 fetches vs 31 with the fix) and risking a budget-exhaustion miss on gappy chains. Fix: before concluding end-of-book in the exponential phase, fire two single far probes (`at(n+64)`, `at(n+512)`); a hit flows through the same bound logic, re-establishing bisection. Cost at true end-of-book is 2 extra fetches, once per seek — sweeping here would cost 16 at every true end (measured live: a BDB link seek already costs ~12s; sweeps would add ~5s each). A false end merely degrades to the linear walk, never a miss. Bisect-phase 404 handling (`hi = mid`, recoverable) is already correct and stays. Far probes go through `_fetch_deadline` (§4.A) and count against the budget.

### 4.E Control-flow hygiene

- Replace `assert headword` (handler) and `assert lo_id is not None …` (`_seek_numeric`) with explicit raises. `python -O` strips asserts; the second documents an invariant that deserves a real error.
- Delete the dead `or not letter_idx` clause in `_plan_scan_ranges`. Traced: with zero letter sections `supplement` computes to `[]`, so the clause can't fire — it only misleads readers into thinking letterless books get full-supplement walks. (The scary version — accidentally walking a whole commentary — was checked and can't happen.)
- Fix the `_seek_numeric` forward-walk guard `budget[0] >= 0` → `> 0`: at zero budget the walk takes one uncounted step and `scan_complete` semantics (budget exhausted ⇒ incomplete) go fuzzy.

### 4.F Waste removal + budget honesty

- Return the article cache from `_resolve_candidates` (`-> tuple[candidates, scan_complete, cache]`) and serve the single-hit winner from it instead of re-fetching — one saved request per call.
- Thread `(cache, budget, deadline)` into `_recover_numeric`: today's probe envelope is fetched, discarded, and re-fetched on walk entry. Store it; count it.
- Thread `(budget, deadline)` into `_recover_by_alpha_scan` and `_alpha_offset_map`: their probes (up to 30 + 27 GETs) are currently free — `MAX_WALK_ARTICLES` undercounts alpha-chain calls badly. Cache hits stay free.
- `_seek_alpha`: check `cache` before fetching (today it always re-fetches and overwrites — repeated seeks burn budget on the same articles).
- Skip the book+TOC fetch on the direct `article_id` path (title comes from the article envelope's `resourceTitle`, falling back to the book fetch only when absent).

### 4.G Documentation of discovered constants

- `language` codes: `he` and `arc` observed live. `el` is expected for Greek lexicons but **unconfirmed** — the current schema already lists it as observed; downgrade the description to `he`/`arc` until verified against LSJ or similar, then re-add. Extend the list when new codes appear in the wild (unknown codes resolve to `not_found` once the §4.C filter fix lands).
- `continuation.next_start` units: characters (Python `str` units) into the article HTML. State it in the schema.
- Mark-order in served `data-headword` attrs is per-resource, not canonical: BDB emits non-NFC order (shin-dot before sheva, dagesh before qamats — verified on מִשְׁפָּט `LBDB.2184.4`, so an NFC-typed query honestly tiers `normalized`), CHALOT emits canonical order (`match: exact` on צְדָקָה `X.50`). Live tests compare served headwords NFC-normalized and pin the BDB tier as `normalized` (tripwire: if Logos normalizes, the pin fails and we re-pin). Never "fix" this by normalizing inside the tier comparison — the exact/normalized distinction exists for exactly this.

### 4.H Scan-to-end sentinel (new — zero `resourceLength`)

`_plan_scan_ranges` computes `end = ends[i] or resource_length or 0`, then collapses `end <= offset` to `offset + 1`. When the book root omits `resourceLength`, a wanted *last* section becomes a 1-char range (verified: `(9000, 9001)` instead of `(9000, …)`) and its entries are silently missed. Fix: `end = ends[i] or resource_length or _SCAN_TO_END` with `_SCAN_TO_END = sys.maxsize`; drop the `+1` collapse (keep a `max(end, offset + 1)` guard only against malformed TOC ordering where a next-section offset precedes the current one). Walks already terminate at chain end (`nextArticleId` exhausted), so the sentinel is bounded by the book, not the budget.

---

## 5. Failure modes

- **Mid-chain gap wider than 8 (numeric).** Capped `hi` kills bisection; the seek degrades to a linear walk and may exhaust the budget (§4.D restores bisection via far probes — 98→31 fetches measured).
- **Budget exhaustion (`MAX_WALK_ARTICLES = 5_000`).** Returns `scan_complete: false` with whatever was found. Callers must treat a lone entry with `scan_complete: false` as provisional (§4.C makes this visible). After §4.F the budget counts every resolution fetch, including recovery and alpha probes.
- **Deadline (§4.A).** Same shape as budget exhaustion. Link-seek results precede the scan, so the timeout response degrades to "word-study answer + partial scan" when word study answered. A hung fetch resolves to expiry via `wait_for(remaining)`, never to a dead call; the httpx 120s client timeout remains as a backstop outside resolution.
- **Book root is the reader's last-read position.** Only the chain *prefix format* is taken from it for seeking (exponential search always starts from the top of the book), so mid-book roots can't truncate seeks. Exception: `_seek_offset`'s last-resort fallback walks forward *from* the hint — kept deliberately; on a mid-book hint past the target it returns `None` (unfound), never a wrong article.
- **Stale word-study offsets.** A link may point at an article whose headwords no longer match (re-index). It surfaces as `match: "offset"` — returned, never silently promoted to an exact hit.
- **Word-study outage.** `_lemma_links` catches all exceptions and returns `([], None)` — resolution degrades to scan-only. Consequence: Aramaic-supplement entries become unreachable (no `guide_ref`, no out-of-section links, so supplements are excluded by `_plan_scan_ranges`). Conservative and correct — blind supplement walks are the expensive fallback the planner deliberately avoids — but callers should know outage + Aramaic headword ⇒ likely miss.
- **Zero-offset articles on alpha chains.** `_scan_section_alpha`'s end condition ignores `off == 0` (`off >= end and off > 0`); front-matter articles with zero offsets can't terminate the walk — chain end and budget do. Bounded, but such walks run longer than their section.
- **Live index drift.** The opt-in integration tests pin `indexed_offset` values; if Logos re-indexes, those tests rot while the tool still works. That's intended — the pins are tripwires, and they're opt-in (`LOGOS_LIVE=1`) so they never break the default suite.

## 6. File-by-file implementation plan (v2)

- `logos/tools/get_entry.py` — §§4.A–4.H. Touch points: `handler` (`timeout_s` input + schema, `status`, direct-path skip, explicit raise, serve winner from cache); `_entry_response` (+`status`/`match`/`scan_complete`); `_candidate_row` (+fallback label, matched span language); `_resolve_candidates` (+`deadline`, return cache); `_fetch_deadline` (new helper); `_seek_numeric` (deadline, far probes, explicit raise, `> 0` guard); `_seek_alpha` (cache check, deadline); `_seek_offset` (deadline); `_walk_range_numeric` / `_scan_section_numeric` / `_scan_section_alpha` (deadline); `_recover_numeric` / `_recover_by_alpha_scan` / `_alpha_offset_map` (+cache/budget/deadline); `_plan_scan_ranges` (delete dead clause, scan-to-end sentinel). No new modules.
- `tests/unit/test_get_entry.py` — §7 additions; extend the contract test for `status`.
- `tests/integration/test_get_entry_live.py` — **changes**: assert `status`/`match`/`scan_complete` on the BDB pin; add CHALOT צְדָקָה pin, ambiguous-שער gate+hair live check, and `timeout_s: 10`/`30` partial-response checks. Pins stand. Plus an autouse fixture closing the `logos_client` singleton after each test: its httpx pool binds to the first test's event loop and pytest-asyncio runs each test on a fresh loop, so every test after the first died with "Event loop is closed" until the fixture. Production (single-loop MCP server) is unaffected.
- `pack.yaml` — unchanged (input schema is code-declared).

## 7. Tests

Keep everything green now, and add the tests v1 is missing — the highest-risk machinery (gap-tolerant bisect, chunk merge, exhaustion) is structurally untested:

1. **Mid-chain gap:** 200-article fake numeric chain, articles 64–71 404, target at 15010. Asserts the target is found in <60 article fetches (v1's capped-`hi` walk burns 98) — i.e. far probes re-established bisection.
2. **Two-chunk boundary:** fake section spanning 2 chunks (patch `_CHUNK_TARGET_BYTES` small) with matches on both sides of the boundary and one straddling seed. Asserts no miss, no duplicate.
3. **Zero-budget:** `MAX_WALK_ARTICLES` patched near-exhausted. Asserts `scan_complete: false` and link-seek candidates still present.
4. **Deadline (§4.A):** hanging article endpoint (`await event.wait()` on a never-set event) on the scan path, fast link-seek endpoints; `timeout_s=0.5`. Asserts return within `timeout_s + 5s` slack with `scan_complete: false` and the link-seek candidate present.
5. **Contract:** `status` present and correct on all three shapes; `match` + `scan_complete` on entries; candidate `headword` never empty; `language` equals the matched span's; compat keys (`found`/`ambiguous`) still present until 0.2.0.
6. **Zero-length sentinel (§4.H):** book with `resourceLength` 0/omitted, wanted last section. Asserts the range end is `_SCAN_TO_END` and the entry is found.
7. Existing: verbatim preservation, gate+hair ambiguity, pointed homonyms, alpha-chain walk, truncation/continuation, Strong's empty, language filter, no-TOC-deref, import/dry-run read-only proofs, pack registration. None of these change meaning under v2 (ambiguous/not-found assertions gain `status` checks).

## 8. Non-goals and acceptance

- No persistent cache. Repeated survey calls re-walk; that cost was accepted to hold the no-writes constraint. Revisit only if a caller demonstrates repeated identical lookups AND the constraint is explicitly lifted — a cache is a write. (The in-memory alpha probe map stays: memoization, not persistence.)
- No `resolve`/`fetch` tool split. The scan is inherent to correctness (homonyms), so splitting only moves the latency, never removes it. The deadline (§4.A) is the answer to latency, not a new endpoint.
- No HTML→text conversion. Consumers that want plain text strip tags themselves; the tool's contract is verbatim HTML.

**Acceptance for v2:**

```bash
# unit suite + lint (default, no network)
uv run pytest tests/unit/test_get_entry.py -q
uv run ruff check logos/tools/get_entry.py tests/unit/test_get_entry.py tests/integration/test_get_entry_live.py

# live pins (needs seeded Logos session via logos-login)
LOGOS_LIVE=1 uv run pytest tests/integration/test_get_entry_live.py -q
```

- `logos.get_entry({resource_id: "LLS:46.30.16", headword: "מִשְׁפָּט"})` returns BDB's entry verbatim (`status: entry`, `match: normalized` — BDB's non-canonical mark order, §4.G —, `scan_complete: true`; offsets 5710842/5265 pinned).
- `({resource_id: "LLS:CNCSHAL", headword: "צְדָקָה"})` returns CHALOT's entry verbatim (`status: entry`, `match: exact`, `scan_complete: true`; article `X.50`, offsets 1067647/824 pinned 2026-09-12).
- Ambiguous שער returns gate+hair candidates (`status: ambiguous`, 17 candidates all `consonantal`; gate noun pinned as `LBDB.2178.1`/he/5690376, hair as `LBDB.2504.1`/arc/6041176 with gloss `hair`; non-empty headwords, glosses).
- `timeout_s: 10` on the שער lookup returns in under ~20s with `scan_complete: false` (shaped, never dead); `timeout_s: 30` additionally carries the hair candidate (a BDB link seek alone costs ~12s live, so 10s can't promise hair). Unit test pins the mechanism deterministically at `timeout_s=0.5`.
- Full suite green with no corpus side effects; ruff-clean.
