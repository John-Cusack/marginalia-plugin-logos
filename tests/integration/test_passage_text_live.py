"""Live-API tests for logos.passage_text (opt-in, read-only).

Tripwire for the comparisonV2 request contract: the API silently ignores
unknown body fields and returns the account's saved default panel, so only
a live call can catch the next field rename. Mocks pin the shape; these
pin the server.

Run with::

    LOGOS_LIVE=1 uv run pytest tests/integration/test_passage_text_live.py -q

Requires a seeded Logos session (``logos-login``).
"""

import json
import os

import pytest

pytestmark = [pytest.mark.integration]

LIVE = os.environ.get("LOGOS_LIVE") == "1"
needs_live = pytest.mark.skipif(not LIVE, reason="needs LOGOS_LIVE=1")


@pytest.fixture(autouse=True)
async def _fresh_http_client():
    """Own httpx pool per test (see test_get_entry_live for why)."""
    yield
    from logos.http.client import logos_client

    await logos_client.close()


def _first_verse(data: dict) -> dict:
    for comparison in data["comparisons"]:
        if comparison.get("kind") == "verseContent":
            return comparison["verseContent"]
    raise AssertionError("no verseContent in comparisons")


@needs_live
async def test_live_luke_single_esv_echoes_request():
    from logos.tools.passage_text import handler

    data = json.loads(await handler(reference="Luke 3:12-14", versions=["ESV"]))

    assert data["referenceInput"] == "Luke 3:12–14"
    assert data["reference"]["raw"].startswith("bible.63.3.12")
    verse = _first_verse(data)
    assert verse["reference"]["rendered"] == "Luke 3:12"
    assert [r["resourceId"] for r in verse["resources"]] == ["LLS:1.0.710"]
    assert "Tax collectors" in verse["resources"][0]["text"]


@needs_live
async def test_live_daniel_is_not_luke_or_john():
    from logos.tools.passage_text import handler

    luke = json.loads(await handler(reference="Luke 3:12-14", versions=["ESV"]))
    daniel = json.loads(await handler(reference="Daniel 4:27", versions=["ESV"]))

    assert daniel["referenceInput"] == "Daniel 4:27"
    assert daniel["reference"]["raw"].startswith("bible.27.4.27")
    assert daniel["referenceInput"] != luke["referenceInput"]
    assert "John 1" not in (luke["referenceInput"], daniel["referenceInput"])
    verse = _first_verse(daniel)
    assert [r["resourceId"] for r in verse["resources"]] == ["LLS:1.0.710"]
    assert "counsel be acceptable" in verse["resources"][0]["text"]


@needs_live
async def test_live_short_raw_reference_accepted():
    from logos.tools.passage_text import handler

    data = json.loads(await handler(reference="bible.64.3.16", versions=["ESV"]))
    assert data["referenceInput"] == "John 3:16"
