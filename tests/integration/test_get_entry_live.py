"""Live-API tests for logos.get_entry (opt-in, no writes).

Hits the real Logos API for BDB ``LLS:46.30.16`` and CHALOT ``LLS:CNCSHAL``
and asserts verbatim-entry retrieval, ambiguity pick-lists, and the
deadline partial-response shape. The tool is read-only by construction
(see the import/dry-run tests in tests/unit/test_get_entry.py): it opens
no DB connections and creates no corpus rows — it only GETs the public
Logos app API.

Run with::

    LOGOS_LIVE=1 uv run pytest tests/integration/test_get_entry_live.py -q

Requires a seeded Logos session (``logos-login``) like every other live
call. Full-answer tests pass ``timeout_s=300`` — an effective backstop,
not a deadline — because complete letter-section walks run past the
default 55s (BDB מִשְׁפָּט ~109s, BDB שער ~187s observed).
"""

import json
import os
import time
import unicodedata


import pytest

pytestmark = [pytest.mark.integration]

LIVE = os.environ.get("LOGOS_LIVE") == "1"
needs_live = pytest.mark.skipif(not LIVE, reason="needs LOGOS_LIVE=1")

BDB = "LLS:46.30.16"
CHALOT = "LLS:CNCSHAL"

@pytest.fixture(autouse=True)
async def _fresh_http_client():
    """Give every test its own httpx pool on its own event loop.

    The ``logos_client`` singleton binds its AsyncClient to the running
    loop on first use, but pytest-asyncio runs each test on a fresh loop —
    reusing the pool across tests fails with "Event loop is closed".
    Closing after each test forces a lazy reopen on the next test's loop.
    """
    yield
    from logos.http.client import logos_client

    await logos_client.close()


@needs_live
async def test_live_bdb_mishpat_verbatim():
    from logos.tools.get_entry import handler

    raw = await handler(resource_id=BDB, headword="מִשְׁפָּט", timeout_s=300)
    data = json.loads(raw)

    assert data["status"] == "entry"
    # BDB's data-headword attrs emit points in non-canonical mark order
    # (shin-dot before sheva, dagesh before qamats — verified 2026-09-12:
    # served '\u05de\u05b4\u05e9\u05c1\u05b0\u05e4\u05bc\u05b8\u05d8'), so an NFC-typed query
    # honestly tiers as "normalized", never "exact". Pin it as a tripwire:
    # if Logos ever serves canonical order this fails and we re-pin.
    assert data["match"] == "normalized"
    assert unicodedata.normalize("NFC", data["headword"]) == "מִשְׁפָּט"
    assert data["scan_complete"] is True
    assert data["resource_title"].startswith("Enhanced Brown-Driver-Briggs")
    # Known word-study lemma-link values for BDB מִשְׁפָּט.
    assert data["indexed_offset"] == 5710842
    assert data["indexed_length"] == 5265
    assert "מִשְׁפָּט" in data["text"]
    assert data["truncated"] is False


@needs_live
async def test_live_chalot_tsedaqa_verbatim():
    from logos.tools.get_entry import handler

    raw = await handler(resource_id=CHALOT, headword="צְדָקָה", timeout_s=300)
    data = json.loads(raw)

    assert data["status"] == "entry"
    assert data["match"] == "exact"
    assert data["scan_complete"] is True
    assert data["resource_title"]
    assert "צְדָקָה" in data["text"]
    assert data["truncated"] is False
    # Observed values, pinned as a re-index tripwire (cf. the BDB pin).
    # Unlike BDB, CHALOT serves canonical mark order, so the match is exact.
    assert data["article_id"] == "X.50"
    assert data["indexed_offset"] == 1067647
    assert data["indexed_length"] == 824


@needs_live
async def test_live_shaar_ambiguous_gate_and_hair():
    from logos.tools.get_entry import handler

    raw = await handler(resource_id=BDB, headword="שער", timeout_s=300)
    data = json.loads(raw)

    assert data["status"] == "ambiguous"
    assert data["scan_complete"] is True
    assert all(c["headword"] for c in data["candidates"])
    assert all(c["gloss"] for c in data["candidates"])
    by_id = {c["article_id"]: c for c in data["candidates"]}
    # Gate noun: BDB points it with a shin dot (I. שער, S 8179/8180,
    # n. m.), so the queried pointing never matches the served bytes —
    # pin the article id + offset (re-index tripwire, cf. §5), not bytes.
    assert "LBDB.2178.1" in by_id
    assert by_id["LBDB.2178.1"]["language"] == "he"
    assert by_id["LBDB.2178.1"]["indexed_offset"] == 5690376
    # Hair: Aramaic-supplement row, word-study-endorsed (offset pinned §5).
    # Served mark order is compared NFC-normalized (cf. the mishpat pin).
    assert "LBDB.2504.1" in by_id
    hair = by_id["LBDB.2504.1"]
    assert hair["language"] == "arc"
    assert hair["indexed_offset"] == 6041176
    assert unicodedata.normalize("NFC", hair["headword"]) == "שְׂעַר"
    assert hair["gloss"] == "hair"


@needs_live
async def test_live_shaar_timeout_returns_partial():
    from logos.tools.get_entry import handler

    # Tight deadline: path 1 may not even finish — still a shaped
    # response with scan_complete=false, never a dead call.
    t0 = time.monotonic()
    raw = await handler(resource_id=BDB, headword="שער", timeout_s=10)
    elapsed = time.monotonic() - t0
    data = json.loads(raw)
    assert elapsed < 20
    assert data["scan_complete"] is False
    assert data["status"] in ("entry", "ambiguous", "not_found")

    # Roomy deadline: the word-study-endorsed hair article resolves and
    # survives the expiry — as a provisional lone entry or in a pick-list.
    # (A BDB link seek alone costs ~12s live, so 10s can't promise hair.)
    t0 = time.monotonic()
    raw = await handler(resource_id=BDB, headword="שער", timeout_s=30)
    elapsed = time.monotonic() - t0
    data = json.loads(raw)
    assert elapsed < 45
    assert data["scan_complete"] is False
    if data["status"] == "entry":
        assert unicodedata.normalize("NFC", data["headword"]) == "שְׂעַר"
    else:
        assert data["status"] == "ambiguous"
        normed = {unicodedata.normalize("NFC", c["headword"]) for c in data["candidates"]}
        # "שְׂעַר" is itself NFC-stable; the served bytes may not be.
        assert "שְׂעַר" in normed
