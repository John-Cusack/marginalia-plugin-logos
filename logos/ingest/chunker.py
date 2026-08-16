"""Verse-boundary-aware text chunker for commentary content."""

from __future__ import annotations

import re

from research_engine.domain.passages import PassageDraft
from research_engine.plugins.sdk.interfaces import Chunker
from research_engine.services.ingestion.chunking.fixed_window import (
    cap_spans,
    split_at_boundary,
)
from research_engine.services.text.tokens import (
    approx_tokens,
    chars_per_token,
    token_budget_chars,
)

from logos.ingest.scripture_refs import extract_scripture_refs

MAX_CHUNK_TOKENS = 500
OVERLAP_TOKENS = 50

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
    # 4.0: the cap is measured in real tokens. This chunker's whole reason to
    # exist is Greek and Hebrew reference works, and 4 chars per token is an
    # English constant — BDAG runs at 1.83 and HALOT at 1.50, so a "500 token"
    # chunk of either held more than twice what it claimed.
    version = "4.0"

    @property
    def max_passage_tokens(self) -> int | None:
        return MAX_CHUNK_TOKENS

    async def chunk(self, text: str, metadata: dict | None = None) -> list[PassageDraft]:
        if not text.strip():
            return []

        rate = chars_per_token(text)
        base_metadata = dict(metadata) if metadata else {}
        # The budget comes from the article's average density; `cap_spans` then
        # re-splits the spans where that average was optimistic — a BDAG entry
        # is mostly Latin apparatus by character count and mostly Greek by token
        # cost, so the two differ by more than a factor of two.
        spans = _chunk_spans(
            text,
            token_budget_chars(MAX_CHUNK_TOKENS, rate),
            token_budget_chars(OVERLAP_TOKENS, rate),
        )
        return [
            self._make_draft(text, start, end, position, base_metadata, rate)
            for position, (start, end) in enumerate(spans)
        ]

    def _make_draft(
        self,
        text: str,
        start: int,
        end: int,
        position: int,
        base_metadata: dict,
        rate: float,
    ) -> PassageDraft:
        chunk_text = text[start:end]
        scripture_refs = extract_scripture_refs(chunk_text)
        chunk_metadata = {**base_metadata, "scripture_refs": scripture_refs}

        return PassageDraft(
            position=position,
            char_start=start,
            char_end=end,
            text=chunk_text,
            token_count=approx_tokens(chunk_text, rate),
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


def _chunk_spans(
    text: str, max_chars: int, overlap_chars: int
) -> list[tuple[int, int]]:
    """Chunk spans over *text*, verse-section first and paragraph-split if long.

    *max_chars* and *overlap_chars* are the token budgets converted for this
    text's script, so the same call yields ~500-token chunks in Greek as in
    English rather than ~500-token chunks of English and ~1,100 of Greek.
    """
    base: list[tuple[int, int]] = []
    for section_start, section_end in _verse_section_spans(text):
        if section_end - section_start <= max_chars:
            base.append((section_start, section_end))
        else:
            base.extend(_paragraph_spans(text, section_start, section_end, max_chars))

    # Capping happens here, on spans that do not yet overlap. Doing it after the
    # overlap pass below reorders the result: a widened span reaches back behind
    # the sub-spans an earlier one was just cut into, and the contract's
    # "spans go backwards" assertion is what caught that.
    base = cap_spans(text, base, MAX_CHUNK_TOKENS)

    spans: list[tuple[int, int]] = []
    for i, (start, end) in enumerate(base):
        if i > 0:
            # Overlap: reach back into the preceding text rather than copying it.
            start = max(0, start - overlap_chars)
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


def _paragraph_spans(
    text: str, start: int, end: int, max_chars: int
) -> list[tuple[int, int]]:
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
        elif para_end - current[0] > max_chars:
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
        for piece in split_at_boundary(region, s, e, max_chars)
    ]

    return [(start + s, start + e) for s, e in absolute]


