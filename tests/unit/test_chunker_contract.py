"""VerseChunker against the engine's shared chunker contract.

Imported from `research_engine_sdk.testing` rather than restated here. The pack's
chunker had drifted from the contract for its whole life — no declared cap, no
size test — and was only caught by `research-engine doctor` reading the live
corpus, at 95 passages over 1,200 tokens. Running the engine's own assertion
here is what makes that a build failure rather than a discovery.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from research_engine_sdk.testing import assert_chunker_contract

from logos.ingest.chunker import VerseChunker
from tests.unit.chunker_fixtures import ALL_TEXTS, CORE_0_5_TEXTS, LOGOS_TEXTS

pytestmark = pytest.mark.unit


async def test_verse_chunker_satisfies_the_engine_contract() -> None:
    await assert_chunker_contract(VerseChunker())


@pytest.mark.parametrize("name", sorted(CORE_0_5_TEXTS))
async def test_verse_chunker_still_holds_the_0_5_contract_texts(name: str) -> None:
    """The SDK's contract set dropped shapes core 0.5.0 checked; keep checking them."""
    await assert_chunker_contract(VerseChunker(), texts={name: CORE_0_5_TEXTS[name]})


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


@pytest.mark.parametrize("name", sorted(LOGOS_TEXTS))
async def test_verse_chunker_holds_the_contract_on_logos_shapes(name: str) -> None:
    """English, Greek, Hebrew, a lexicon paragraph, indexes, and the overlap case,
    each by name so a failure says which shape broke."""
    await assert_chunker_contract(VerseChunker(), texts={name: LOGOS_TEXTS[name]})


#: sha256 of each fixture's drafts as emitted by VerseChunker 5.0 while it still
#: imported core's chunking helpers (research-engine 0.5.0), before the move to
#: `research_engine_sdk.chunking`. Equal digests mean the SDK cut-over changed no
#: passage boundary, token count or metadata, so no reindex is owed. A deliberate
#: boundary change bumps `VerseChunker.version` and regenerates these.
CHUNKER_5_0_DIGESTS = {
    "book_scale": "31e51ebc4812e2cf825749750aa5d769b230727fee0b05ef2240653a8cf6979a",
    "cjk": "58a3e525e186160ec76c8102bb7dd1f4360911a78d0a1ab2d6ee599b071cab60",
    "crlf": "33450438bfe1894d5e8fe5478f7fd9398798da987d777a90a6efb8b299562600",
    "empty": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
    "english_commentary": "912b90b06663e5dda4f8cb650b91239487041540dc61be5abad5f968afeb1053",
    "greek": "232f067bbabccc5ef7045bedabae07e2e41aaa377052dacc86b7fc3537109cb4",
    "hebrew": "87161beb53e618e64ac6a193cf13037b47581a9fbcf5e548ed57f402386a80fd",
    "index": "a2c574311e94cf3e6b5cdfae85147200e3300aece7290eda9d9189be5ebca36c",
    "lexicon": "6b97cab4ec64a1d9450c8a46acb455632234931c5132713583847071bf981998",
    "lexicon_paragraph": "f353efff74c6dbd940497c477403e2a50d073a5994d882d344272a7c2bf86e68",
    "no_boundaries": "c1d961f75cf193e57e1923106e49ff742c321277fbb0b02e6c361be5af22cd53",
    "overlap_amplification": "b989877b0e26549b7bd1c206cf9b0cd5ba4106420992523f3292735dd8dfbc6f",
    "prose": "ef7659efd5424dac86ae4479be92bc8d38f203fad5803803c6627b68cf489dda",
    "scripture_index": "09d56af6f9fbe8468162bf0fbf69226fe1c8b1f3010ea0f54a8d57615283caee",
    "single_sentence": "45c5ce1edd821d346506fdda8c2229d2a8687206bfb60362d03fc424e621c61b",
    "whitespace_only": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
}


@pytest.mark.parametrize("name", sorted(ALL_TEXTS))
async def test_the_sdk_cut_over_left_every_boundary_where_5_0_put_it(name: str) -> None:
    assert VerseChunker.version == "5.0"
    drafts = await VerseChunker().chunk(
        ALL_TEXTS[name], {"resource_id": "LLS:X", "article_id": "A.1"}
    )
    dumped = json.dumps(
        [d.model_dump(mode="json") for d in drafts], ensure_ascii=False, sort_keys=True
    )
    assert hashlib.sha256(dumped.encode()).hexdigest() == CHUNKER_5_0_DIGESTS[name]
