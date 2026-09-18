"""The texts VerseChunker is held to, beyond the SDK's own contract texts.

Each is a shape this chunker exists for or has broken on. `CORE_0_5_TEXTS` is
the contract set core 0.5.0 held every chunker to, kept verbatim here because
`research_engine_sdk.testing.CONTRACT_TEXTS` is a different, smaller set: it has
no scripture index, no Hebrew/Latin lexicon entry and no book-scale text, and
its Greek and index texts differ. The 5.0 digests were taken over these.
"""

from __future__ import annotations


#: Pointed Hebrew with verse-opened notes: the densest script the corpus holds.
HEBREW = (
    "וַיֹּאמֶר אֱלֹהִים יְהִי אוֹר וַיְהִי־אוֹר׃ " * 30
    + "\n\n1:4 וַיַּרְא אֱלֹהִים אֶת־הָאוֹר כִּי־טוֹב\n\n"
) * 40

#: One unbroken lexicon paragraph mixing Greek, Hebrew and Latin apparatus —
#: mostly ASCII by character count, mostly Greek and Hebrew by token cost.
LEXICON_PARAGRAPH = (
    "λόγος, ου, ὁ (Hom.+) — 1. a communication whereby the mind finds expression, "
    "word; cp. Ex. 3:14; Gn. 1:1; בְּ Sem. בָּרָא Akk. bašū (cf. AHw. 112) "
) * 400

#: A scripture index with a page column: every line is a verse boundary, which
#: is what once turned 38,042 characters into 844 overlapping passages.
OVERLAP_AMPLIFICATION = "".join(
    f"{chapter}:{verse}\tpp. {chapter * 3 + verse}\n"
    for chapter in range(1, 60)
    for verse in range(1, 20)
)

#: English commentary keyed on verse references, with paragraph breaks.
ENGLISH_COMMENTARY = (
    "3:16 For God so loved the world, that he gave his only begotten Son, "
    "that whosoever believeth in him should not perish.\n\n"
    + "This verse summarises the gospel. " * 30
    + "\n\nv. 17 For God sent not his Son.\n\n"
) * 25

CORE_0_5_TEXTS: dict[str, str] = {
    "empty": "",
    "whitespace_only": "   \n\n\t  ",
    "single_sentence": "One sentence with no terminator",
    "prose": (
        "The archive holds letters. Each letter has a date. "
        "Some dates are approximate. Others are exact. "
    ) * 40,
    # No sentence punctuation at all, newline-delimited records.
    "index": "Index\n\nPage numbers correspond to the print edition.\n\n"
    + "".join(
        f"{word}, {n}, {n + 40}\n"
        for n, word in enumerate(
            ["abolition", "archive", "binding", "clerk", "codex", "deed"] * 400
        )
    ),
    # A scripture index: every line opens with a verse reference. The existing
    # `index` fixture above does not catch this, because its lines read
    # "abolition, 0, 40" — no colon, so a chunker keyed on verse references
    # finds no boundary and the fixture proves nothing about it.
    #
    # Here every line is a boundary, so the sections are one line long and the
    # overlap pass widens each far past its own length.
    "scripture_index": "SCRIPTURE INDEX\n\nGenesis\n\n"
    + "".join(
        f"{chapter}:{verse}\t{chapter * 7 + verse}\u2013{chapter * 7 + verse + 2}\n"
        for chapter in range(1, 51)
        for verse in range(1, 13)
    ),
    # One unbroken paragraph, dense with abbreviations that look like sentence
    # ends but are not — the shape of a lexicon entry.
    "lexicon": (
        "בְּ Sem., Ug. UM §10:1, Akk. in bašū (cf. AHw. 112); cf. Arm. b-, "
        "Syr. b-, Mnd. b-, e.g. v. 3, cp. Gn. 1:1, Ex. 3:14, etc. "
    ) * 200,
    "no_boundaries": "word " * 2000,
    # Sized so that a chunker estimating 4 chars per token — the English
    # constant — emits a passage well past `ABSOLUTE_MAX_TOKENS` in real ones.
    # The earlier, shorter version of this fixture passed against an estimator
    # that had been sabotaged back to English-only, which made it worthless: it
    # asserted the invariant without ever reaching it.
    "cjk": "第一句話寫在這裡。第二句話也寫在這裡，內容比較長一些。" * 900,
    # Polytonic Greek, the shape of a lexicon body. At ~1.8 chars per token this
    # is roughly twice as many tokens as its character count suggests.
    "greek": (
        "λόγος, ου, ὁ (Hom.+) πρὸς τὸν θεόν, καὶ θεὸς ἦν ὁ λόγος· "
        "οὗτος ἦν ἐν ἀρχῇ πρὸς τὸν θεόν. πάντα διʼ αὐτοῦ ἐγένετο. "
    ) * 600,
    "crlf": "First line.\r\nSecond line.\r\n\r\nThird.",
    "book_scale": (
        "The clerk recorded the transaction in the ledger. "
        "A second hand annotated the margin some years later. "
    ) * 3000,
}


LOGOS_TEXTS: dict[str, str] = {
    "english_commentary": ENGLISH_COMMENTARY,
    "greek": CORE_0_5_TEXTS["greek"],
    "hebrew": HEBREW,
    "lexicon_paragraph": LEXICON_PARAGRAPH,
    "index": CORE_0_5_TEXTS["index"],
    "scripture_index": CORE_0_5_TEXTS["scripture_index"],
    "overlap_amplification": OVERLAP_AMPLIFICATION,
}

ALL_TEXTS: dict[str, str] = {**CORE_0_5_TEXTS, **LOGOS_TEXTS}
