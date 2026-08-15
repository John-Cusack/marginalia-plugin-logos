"""Tests for verse-boundary chunker."""

import pytest

from logos.ingest.chunker import VerseChunker


@pytest.fixture
def chunker():
    return VerseChunker()


@pytest.mark.asyncio
async def test_empty_text(chunker):
    result = await chunker.chunk("")
    assert result == []


@pytest.mark.asyncio
async def test_single_short_section(chunker):
    text = "This is a simple commentary section about Romans 3:23."
    result = await chunker.chunk(text)
    assert len(result) == 1
    assert result[0].chunker == "verse_boundary"
    assert result[0].chunker_version == VerseChunker.version
    assert "Rom 3:23" in result[0].metadata["scripture_refs"]


@pytest.mark.asyncio
async def test_verse_boundary_splitting(chunker):
    text = """Introduction to the passage.

3:16 For God so loved the world. This verse is central.

3:17 For God did not send his Son into the world to condemn.

3:18 Whoever believes in him is not condemned."""
    result = await chunker.chunk(text)
    assert len(result) >= 2  # Should split on verse refs


@pytest.mark.asyncio
async def test_scripture_refs_in_metadata(chunker):
    text = "Paul echoes Genesis 3:6 and cites Psalm 14:3 here. Also see Romans 1:17."
    result = await chunker.chunk(text)
    assert len(result) >= 1
    refs = result[0].metadata["scripture_refs"]
    assert "Gen 3:6" in refs
    assert "Ps 14:3" in refs
    assert "Rom 1:17" in refs


@pytest.mark.asyncio
async def test_metadata_passthrough(chunker):
    text = "Short commentary on John 3:16."
    metadata = {"resource_id": "test-resource", "author": "Test Author"}
    result = await chunker.chunk(text, metadata)
    assert len(result) == 1
    assert result[0].metadata["resource_id"] == "test-resource"
    assert result[0].metadata["author"] == "Test Author"
    assert "scripture_refs" in result[0].metadata


@pytest.mark.asyncio
async def test_large_section_splits_on_paragraphs(chunker):
    # Create a section larger than MAX_CHUNK_CHARS (2000 chars)
    para = "This is a paragraph about Romans 8:28. " * 20  # ~800 chars
    text = f"{para}\n\n{para}\n\n{para}\n\n{para}"  # ~3200 chars
    result = await chunker.chunk(text)
    assert len(result) >= 2  # Should split on paragraphs


@pytest.mark.asyncio
async def test_position_increments(chunker):
    text = """First section about Gen 1:1.

3:16 Second section about John 3:16.

3:17 Third section about John 3:17."""
    result = await chunker.chunk(text)
    positions = [d.position for d in result]
    assert positions == list(range(len(positions)))


@pytest.mark.asyncio
async def test_passage_draft_fields(chunker):
    text = "Commentary discussing Hebrews 11:1."
    result = await chunker.chunk(text)
    assert len(result) == 1
    draft = result[0]
    assert draft.text.strip() != ""
    assert draft.token_count > 0
    assert draft.chunker == "verse_boundary"
    assert draft.chunker_version == VerseChunker.version
    assert "locator" in type(draft).model_fields
    assert draft.char_start == 0
    assert draft.char_end == len(text)


@pytest.mark.asyncio
async def test_char_offsets_are_real_fields(chunker):
    """Offsets live on the draft, not in `locator`.

    They are an address into the document, so core indexes them as columns;
    `locator` keeps type-specific extras like page and verse.
    """
    text = "Commentary discussing Hebrews 11:1."
    result = await chunker.chunk(text)
    assert len(result) == 1
    assert result[0].char_start == 0
    assert result[0].char_end == len(text)


@pytest.mark.asyncio
async def test_char_offsets_multi_chunk(chunker):
    text = """Introduction to the passage.

3:16 For God so loved the world. This verse is central.

3:17 For God did not send his Son into the world to condemn."""
    result = await chunker.chunk(text)
    assert len(result) >= 2
    for draft in result:
        # The invariant, exactly: no .strip() on either side. A chunk whose text
        # is not its own span cannot be cited, verified, or re-anchored.
        assert text[draft.char_start:draft.char_end] == draft.text
