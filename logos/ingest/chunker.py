"""Verse-boundary-aware text chunker for commentary content."""

from __future__ import annotations

import re

from research_engine.domain.passages import PassageDraft
from research_engine.plugins.sdk.interfaces import Chunker
from research_engine.services.ingestion.chunking.fixed_window import split_at_boundary

from logos.ingest.scripture_refs import extract_scripture_refs

MAX_CHUNK_TOKENS = 500
OVERLAP_TOKENS = 50
CHARS_PER_TOKEN = 4
MAX_CHUNK_CHARS = MAX_CHUNK_TOKENS * CHARS_PER_TOKEN
OVERLAP_CHARS = OVERLAP_TOKENS * CHARS_PER_TOKEN

VERSE_REF_PATTERN = re.compile(
    r"(?:^|\n)(?:\d+:\d+|\b(?:v|verse?|vs?)\.?\s*\d+)", re.IGNORECASE
)

_PARA_SPLIT = re.compile(r"\n\n+")


class VerseChunker(Chunker):
    """Splits commentary text on verse reference boundaries.

    Every draft satisfies ``draft.text == text[draft.char_start:draft.char_end]``.
    That is why overlap is expressed by widening a chunk's span backwards rather
    than by prepending the previous chunk's tail: concatenated text does not
    occur contiguously in the source, so it has no span to address it.
    """

    id = "verse_boundary"
    #: What `chunk()` takes: "text" or "sections".
    consumes = "text"
    # 3.0: a paragraph larger than the cap is broken at a line or word boundary
    # rather than emitted whole. Lexicon entries — HALOT, BDAG — are routinely
    # one unbroken paragraph, so `research-engine doctor` found 95 passages here
    # over 1,200 tokens, the largest 9,243 characters. Passage boundaries change
    # for those resources; 2.0 passages of them are stale.
    version = "3.0"

    @property
    def max_passage_tokens(self) -> int | None:
        return MAX_CHUNK_TOKENS

    async def chunk(self, text: str, metadata: dict | None = None) -> list[PassageDraft]:
        if not text.strip():
            return []

        base_metadata = dict(metadata) if metadata else {}
        return [
            self._make_draft(text, start, end, position, base_metadata)
            for position, (start, end) in enumerate(_chunk_spans(text))
        ]

    def _make_draft(
        self, text: str, start: int, end: int, position: int, base_metadata: dict
    ) -> PassageDraft:
        chunk_text = text[start:end]
        scripture_refs = extract_scripture_refs(chunk_text)
        chunk_metadata = {**base_metadata, "scripture_refs": scripture_refs}

        return PassageDraft(
            position=position,
            char_start=start,
            char_end=end,
            text=chunk_text,
            token_count=_approx_tokens(chunk_text),
            chunker=self.id,
            chunker_version=self.version,
            metadata=chunk_metadata,
            locator={},
        )


def _trim_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Narrow a span past surrounding whitespace.

    Trimming the span rather than the text is what keeps offsets true: stripping
    the slice would leave the span describing a wider region than its text.
    """
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _chunk_spans(text: str) -> list[tuple[int, int]]:
    """Chunk spans over *text*, verse-section first and paragraph-split if long."""
    base: list[tuple[int, int]] = []
    for section_start, section_end in _verse_section_spans(text):
        if section_end - section_start <= MAX_CHUNK_CHARS:
            base.append((section_start, section_end))
        else:
            base.extend(_paragraph_spans(text, section_start, section_end))

    spans: list[tuple[int, int]] = []
    for i, (start, end) in enumerate(base):
        if i > 0:
            # Overlap: reach back into the preceding text rather than copying it.
            start = max(0, start - OVERLAP_CHARS)
            if spans:
                # Keep starts strictly increasing so chunks stay distinguishable.
                start = max(start, spans[-1][0] + 1)
        start, end = _trim_span(text, start, end)
        if end > start:
            spans.append((start, end))
    return spans


def _verse_section_spans(text: str) -> list[tuple[int, int]]:
    """Spans between lines that open with a verse reference."""
    spans: list[tuple[int, int]] = []
    section_start = 0
    position = 0

    for line in text.split("\n"):
        line_start = position
        position += len(line) + 1  # +1 for the newline
        if VERSE_REF_PATTERN.search(line) and text[section_start:line_start].strip():
            spans.append((section_start, line_start))
            section_start = line_start

    if text[section_start:].strip():
        spans.append((section_start, len(text)))

    return spans or [(0, len(text))]


def _paragraph_spans(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Split a long region into paragraph-aligned spans under the size cap."""
    region = text[start:end]
    paragraphs: list[tuple[int, int]] = []
    cursor = 0
    for match in _PARA_SPLIT.finditer(region):
        paragraphs.append((cursor, match.start()))
        cursor = match.end()
    paragraphs.append((cursor, len(region)))

    spans: list[tuple[int, int]] = []
    current: tuple[int, int] | None = None
    for para_start, para_end in paragraphs:
        if current is None:
            current = (para_start, para_end)
        elif para_end - current[0] > MAX_CHUNK_CHARS:
            spans.append(current)
            current = (para_start, para_end)
        else:
            current = (current[0], para_end)
    if current is not None:
        spans.append(current)

    # A paragraph can itself exceed the cap — a lexicon entry is frequently one
    # unbroken block — and accumulating paragraphs never breaks one open. Left
    # here, such a span becomes a passage the embedding model truncates, so most
    # of the entry is stored and unreachable by search.
    absolute = [
        piece
        for s, e in spans
        for piece in split_at_boundary(region, s, e, MAX_CHUNK_CHARS)
    ]

    return [(start + s, start + e) for s, e in absolute]


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)
