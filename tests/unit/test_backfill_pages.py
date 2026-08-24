"""Recovering page numbers for books already in the corpus.

The dangerous failure here is not missing a page — it is writing a wrong one.
A confident wrong page number is worse than an absent one, because nothing
downstream can tell it is wrong; it just quietly sends a reader to the wrong
place. So the guard that refuses to backfill a document whose offsets do not
address the assembled book is the most important thing in this file.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from logos.tools.backfill_pages import _offsets_match, backfill_pages
from logos.tools.ingest_book import page_locator

MARKERS = [
    {"char_position": 0, "page": "12", "raw_ref": "vp.1.12", "volume": "1"},
    {"char_position": 100, "page": "13", "raw_ref": "vp.1.13", "volume": "1"},
    {"char_position": 200, "page": "14", "raw_ref": "vp.1.14", "volume": "1"},
]


class TestPageLocator:
    def test_a_span_inside_one_page(self):
        assert page_locator(MARKERS, 10, 50) == {
            "page_start": "12", "page_end": "12",
            "volume": "1", "page_refs": ["vp.1.12"],
        }

    def test_a_span_crossing_a_page_break_reports_both(self):
        locator = page_locator(MARKERS, 50, 150)
        assert locator["page_start"] == "12"
        assert locator["page_end"] == "13"
        assert locator["page_refs"] == ["vp.1.12", "vp.1.13"]

    def test_a_span_starting_exactly_on_a_marker_takes_that_page(self):
        assert page_locator(MARKERS, 100, 150)["page_start"] == "13"

    def test_a_span_before_the_first_marker_falls_to_it(self):
        """Text ahead of the first page anchor still belongs to that page."""
        assert page_locator(MARKERS, 0, 5)["page_start"] == "12"

    def test_no_markers_means_no_locator_rather_than_a_guess(self):
        assert page_locator([], 10, 50) == {}


class TestOffsetGuard:
    """The property that keeps a legacy per-batch document from being mislabelled."""

    BOOK = "alpha article text. " * 20

    def _passage(self, start, end, text=None):
        return SimpleNamespace(
            char_start=start, char_end=end,
            text=self.BOOK[start:end] if text is None else text,
        )

    def test_offsets_that_read_back_correctly_are_accepted(self):
        passages = [self._passage(i, i + 30) for i in range(0, 300, 30)]
        assert _offsets_match(passages, self.BOOK)

    def test_offsets_addressing_a_different_text_are_rejected(self):
        """A per-batch document's offsets restart at zero for a later article."""
        passages = [self._passage(i, i + 30, text="completely unrelated content")
                    for i in range(0, 300, 30)]
        assert not _offsets_match(passages, self.BOOK)

    def test_a_document_agreeing_only_at_its_start_is_rejected(self):
        """Why samples are spread rather than taken from the front.

        A batch document holding the book's first articles agrees at offset 0
        and diverges later — checking only the first passage would wave it
        through and stamp every later passage with a wrong page.
        """
        passages = [self._passage(0, 30)] + [
            self._passage(i, i + 30, text="diverged") for i in range(30, 300, 30)
        ]
        assert not _offsets_match(passages, self.BOOK)

    def test_no_passages_is_not_a_match(self):
        assert not _offsets_match([], self.BOOK)


class FakeDocuments:
    def __init__(self, docs):
        self.docs = docs

    async def find_by_metadata(self, key, value):
        return self.docs


class FakePassages:
    def __init__(self, by_doc):
        self.by_doc = by_doc
        self.written: list = []

    async def get_by_document(self, document_id):
        return self.by_doc.get(document_id, [])

    async def set_locators(self, updates):
        self.written.extend(updates)
        return len(updates)


class TestBackfill:
    @pytest.mark.asyncio
    async def test_an_unstored_resource_says_so_rather_than_failing(self):
        results = await backfill_pages(
            "LLS:NOPE", passages=FakePassages({}), documents=FakeDocuments([])
        )
        assert results[0]["status"] == "not stored in the corpus"
        assert results[0]["passages_located"] == 0

    @pytest.mark.asyncio
    async def test_a_resource_with_no_staged_pages_writes_nothing(self, monkeypatch):
        async def no_markers(_):
            return {}

        monkeypatch.setattr(
            "logos.tools.backfill_pages.get_article_page_markers", no_markers
        )
        passages = FakePassages({})
        results = await backfill_pages(
            "LLS:X", passages=passages,
            documents=FakeDocuments([SimpleNamespace(id="d1")]),
        )
        assert "recorded a page" in results[0]["status"]
        assert passages.written == []
