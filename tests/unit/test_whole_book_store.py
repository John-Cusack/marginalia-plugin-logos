"""A walked book is stored as one document, with the structure that addresses it.

The corpus this replaces held LSJ as 1,903 documents named "(batch b0000)" and
up — 2,525 documents for thirteen books — because `ingest_drafts` creates one
document per call and the walker called it every hundred chunks. Nothing could
cite an entry, because no row represented one.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from logos.tools.ingest_book import (
    ARTICLE_SEPARATOR,
    _assemble_book,
    _book_sections,
    _entry_title,
    _store_resource,
)

pytestmark = pytest.mark.unit

ENTRY_A = "ἀλληλοκτονέω, *slay each other*, Hp.Ep.17, Arist.Fr.344."
ENTRY_B = "ἀλληλοπάθεια, ἡ, Astrol., *subjection to mutual influence*, Vett.Val.5."
PREFACE = "Preface 1925\n\nMore than eighty years have passed since the first edition."

ARTICLES = [("R.A.1", ENTRY_A), ("R.A.2", ENTRY_B)]


class TestEntryTitle:
    def test_headword_is_taken_from_the_line_the_entry_opens_with(self):
        assert _entry_title(ENTRY_A) == "ἀλληλοκτονέω"
        assert _entry_title(ENTRY_B) == "ἀλληλοπάθεια"

    def test_a_heading_without_a_gloss_keeps_its_whole_line(self):
        assert _entry_title(PREFACE) == "Preface 1925"

    def test_prose_headings_survive_intact(self):
        heading = "1. Abbreviations of the Names of Biblical Books"
        assert _entry_title(f"{heading}\n\nGen\n\nExod") == heading

    def test_a_sentence_with_commas_is_not_chopped_at_the_first_one(self):
        """A comma is only a headword boundary when a headword precedes it."""
        line = (
            "In the following pages, which are chiefly concerned with usage, "
            "the reader will find that the arrangement differs"
        )
        assert _entry_title(line).startswith("In the following pages,")

    def test_empty_text_has_no_title(self):
        assert _entry_title("   \n\n  ") is None


class TestAssembly:
    def test_every_article_span_addresses_the_assembled_text(self):
        text, spans = _assemble_book(ARTICLES)
        for article_id, article_text in ARTICLES:
            start, end = spans[article_id]
            assert text[start:end] == article_text

    def test_articles_are_joined_by_the_separator(self):
        text, _ = _assemble_book(ARTICLES)
        assert text == ENTRY_A + ARTICLE_SEPARATOR + ENTRY_B

    def test_sections_nest_articles_under_their_heading(self):
        text, spans = _assemble_book(ARTICLES)
        sections = _book_sections(
            ARTICLES, spans, {"R.A.1": "Alpha", "R.A.2": "Alpha"}
        )
        headings = [s for s in sections if s.get("article_id") is None]
        entries = [s for s in sections if s.get("article_id") is not None]

        assert [h["heading"] for h in headings] == ["Alpha"]
        assert [e["heading"] for e in entries] == ["ἀλληλοκτονέω", "ἀλληλοπάθεια"]
        # `R.A.1` is three deep, so four under its heading. It used to be two
        # whatever the id said, which is what flattened every book to depth 2.
        assert [e["level"] for e in entries] == [4, 4]
        # The heading must cover the articles beneath it or an outline is wrong.
        assert headings[0]["char_end"] == spans["R.A.2"][1]


def _chunk(article_id: str, text: str, start: int, end: int) -> dict:
    return {
        "id": 1,
        "article_id": article_id,
        "draft_json": json.dumps(
            {
                "position": 0,
                "char_start": start,
                "char_end": end,
                "text": text,
                "chunker": "verse_boundary",
                "chunker_version": "5.0",
                "token_count": 12,
                "metadata": {"heading_path": ["Alpha"], "article_id": article_id},
            }
        ),
    }


@pytest.fixture
def stored():
    """Captures the single ingest_drafts call the storer is expected to make."""
    calls: list[dict] = []
    ingestion = AsyncMock()

    async def capture(**kwargs):
        calls.append(kwargs)
        return {"document_id": str(uuid4()), "passage_count": len(kwargs["passage_drafts"])}

    ingestion.ingest_drafts = AsyncMock(side_effect=capture)

    chunks = [
        _chunk("R.A.1", ENTRY_A, 0, len(ENTRY_A)),
        _chunk("R.A.2", ENTRY_B, 0, len(ENTRY_B)),
    ]
    with (
        patch("logos.tools.ingest_book.get_all_pending_chunks",
              new_callable=AsyncMock, return_value=chunks),
        patch("logos.tools.ingest_book.get_ordered_article_texts",
              new_callable=AsyncMock, return_value=ARTICLES),
        patch("logos.tools.ingest_book.mark_resource_chunks_stored",
              new_callable=AsyncMock, return_value=2),
    ):
        yield ingestion, calls


class TestStoreResource:
    async def test_a_book_becomes_exactly_one_document(self, stored) -> None:
        ingestion, calls = stored
        result = await _store_resource("LLS:46.30.25", ingestion, "A Greek-English Lexicon", {})

        assert len(calls) == 1, "a book stored in pieces is what this replaces"
        assert calls[0]["title"] == "A Greek-English Lexicon"
        assert calls[0]["source"] == "logos:LLS:46.30.25", "no batch in the source"
        assert result["total_stored"] == 2

    async def test_passage_offsets_address_the_assembled_book(self, stored) -> None:
        """The invariant everything else rests on: a quote must verify."""
        ingestion, calls = stored
        await _store_resource("LLS:46.30.25", ingestion, "LSJ", {})

        full_text = calls[0]["full_text"]
        for draft in calls[0]["passage_drafts"]:
            assert full_text[draft.char_start : draft.char_end] == draft.text

    async def test_the_second_article_is_not_left_at_offset_zero(self, stored) -> None:
        """Article-relative offsets all claim to start at 0; rebasing is the fix."""
        ingestion, calls = stored
        await _store_resource("LLS:46.30.25", ingestion, "LSJ", {})

        starts = [d.char_start for d in calls[0]["passage_drafts"]]
        assert starts[1] > 0, "every article would otherwise quote the first one"
        assert len(set(starts)) == 2

    async def test_structure_is_handed_over_with_the_text(self, stored) -> None:
        ingestion, calls = stored
        await _store_resource("LLS:46.30.25", ingestion, "LSJ", {})

        nodes = calls[0]["node_drafts"]
        titles = [n.title for n in nodes]
        assert "LSJ" in titles, "the root carries the book"
        assert "ἀλληλοκτονέω" in titles, "the entry is what a citation names"
        assert all(n.char_end <= len(calls[0]["full_text"]) for n in nodes)

    async def test_chunks_with_no_article_text_are_left_out(self) -> None:
        """Anchoring them to the wrong article would misquote the book."""
        ingestion = AsyncMock()
        ingestion.ingest_drafts = AsyncMock(
            return_value={"document_id": str(uuid4()), "passage_count": 1}
        )
        chunks = [
            _chunk("R.A.1", ENTRY_A, 0, len(ENTRY_A)),
            _chunk("MISSING", "orphan", 0, 6),
        ]
        with (
            patch("logos.tools.ingest_book.get_all_pending_chunks",
                  new_callable=AsyncMock, return_value=chunks),
            patch("logos.tools.ingest_book.get_ordered_article_texts",
                  new_callable=AsyncMock, return_value=[("R.A.1", ENTRY_A)]),
            patch("logos.tools.ingest_book.mark_resource_chunks_stored",
                  new_callable=AsyncMock, return_value=1),
        ):
            result = await _store_resource("LLS:X", ingestion, "Book", {})

        assert result["total_stored"] == 1
        assert result["total_failed"] == 1
