"""VerseChunker against the engine's shared chunker contract.

Imported from `research_engine.testing` rather than restated here. The pack's
chunker had drifted from the contract for its whole life — no declared cap, no
size test — and was only caught by `research-engine doctor` reading the live
corpus, at 95 passages over 1,200 tokens. Running the engine's own assertion
here is what makes that a build failure rather than a discovery.
"""

from __future__ import annotations

import pytest
from research_engine.testing import assert_chunker_contract

from logos.ingest.chunker import VerseChunker

pytestmark = pytest.mark.unit


async def test_verse_chunker_satisfies_the_engine_contract() -> None:
    await assert_chunker_contract(VerseChunker())


async def test_a_lexicon_entry_is_not_emitted_whole() -> None:
    """One unbroken paragraph, the shape that produced the corpus defect."""
    entry = (
        "בְּ Sem., Ug. UM §10:1, Akk. in bašū (cf. AHw. 112); Arm. b-, Syr. b-, "
        "cp. Gn. 1:1, Ex. 3:14, etc. "
    ) * 300
    drafts = await VerseChunker().chunk(entry)

    assert len(drafts) > 1
    assert all(len(d.text) // 4 <= 750 for d in drafts)
    for draft in drafts:
        assert entry[draft.char_start : draft.char_end] == draft.text
