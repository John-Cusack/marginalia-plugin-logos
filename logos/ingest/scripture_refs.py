"""Regex-based Bible reference extractor for scholarly text."""

from __future__ import annotations

import re

# SBL-style book abbreviations mapped to canonical names.
_BOOK_PATTERNS: list[tuple[str, str]] = [
    # Old Testament
    (r"Gen(?:esis)?", "Gen"),
    (r"Exod(?:us)?", "Exod"),
    (r"Lev(?:iticus)?", "Lev"),
    (r"Num(?:bers)?", "Num"),
    (r"Deut(?:eronomy)?", "Deut"),
    (r"Josh(?:ua)?", "Josh"),
    (r"Judg(?:es)?", "Judg"),
    (r"Ruth", "Ruth"),
    (r"1\s*Sam(?:uel)?", "1 Sam"),
    (r"2\s*Sam(?:uel)?", "2 Sam"),
    (r"1\s*Kgs|1\s*Kings", "1 Kgs"),
    (r"2\s*Kgs|2\s*Kings", "2 Kgs"),
    (r"1\s*Chr(?:on(?:icles)?)?", "1 Chr"),
    (r"2\s*Chr(?:on(?:icles)?)?", "2 Chr"),
    (r"Ezra", "Ezra"),
    (r"Neh(?:emiah)?", "Neh"),
    (r"Esth(?:er)?", "Esth"),
    (r"Job", "Job"),
    (r"Pss|Psalms|Ps(?:alm)?", "Ps"),
    (r"Prov(?:erbs)?", "Prov"),
    (r"Eccl(?:es(?:iastes)?)?|Qoh(?:eleth)?", "Eccl"),
    (r"Song|Cant|Song\s*of\s*Sol(?:omon)?", "Song"),
    (r"Isa(?:iah)?", "Isa"),
    (r"Jer(?:emiah)?", "Jer"),
    (r"Lam(?:entations)?", "Lam"),
    (r"Ezek(?:iel)?", "Ezek"),
    (r"Dan(?:iel)?", "Dan"),
    (r"Hos(?:ea)?", "Hos"),
    (r"Joel", "Joel"),
    (r"Amos", "Amos"),
    (r"Obad(?:iah)?", "Obad"),
    (r"Jonah", "Jonah"),
    (r"Mic(?:ah)?", "Mic"),
    (r"Nah(?:um)?", "Nah"),
    (r"Hab(?:akkuk)?", "Hab"),
    (r"Zeph(?:aniah)?", "Zeph"),
    (r"Hag(?:gai)?", "Hag"),
    (r"Zech(?:ariah)?", "Zech"),
    (r"Mal(?:achi)?", "Mal"),
    # New Testament
    (r"Matt(?:hew)?", "Matt"),
    (r"Mark", "Mark"),
    (r"Luke", "Luke"),
    (r"John", "John"),
    (r"Acts", "Acts"),
    (r"Rom(?:ans)?", "Rom"),
    (r"1\s*Cor(?:inthians)?", "1 Cor"),
    (r"2\s*Cor(?:inthians)?", "2 Cor"),
    (r"Gal(?:atians)?", "Gal"),
    (r"Eph(?:esians)?", "Eph"),
    (r"Phil(?:ippians)?", "Phil"),
    (r"Col(?:ossians)?", "Col"),
    (r"1\s*Thess(?:alonians)?", "1 Thess"),
    (r"2\s*Thess(?:alonians)?", "2 Thess"),
    (r"1\s*Tim(?:othy)?", "1 Tim"),
    (r"2\s*Tim(?:othy)?", "2 Tim"),
    (r"Titus", "Titus"),
    (r"Phlm|Philem(?:on)?", "Phlm"),
    (r"Heb(?:rews)?", "Heb"),
    (r"Jas|James", "Jas"),
    (r"1\s*Pet(?:er)?", "1 Pet"),
    (r"2\s*Pet(?:er)?", "2 Pet"),
    (r"1\s*John", "1 John"),
    (r"2\s*John", "2 John"),
    (r"3\s*John", "3 John"),
    (r"Jude", "Jude"),
    (r"Rev(?:elation)?", "Rev"),
]

_BOOK_RE = "|".join(f"(?:{pat})" for pat, _ in _BOOK_PATTERNS)
_REF_PATTERN = re.compile(
    rf"(?<![A-Za-z])"
    rf"({_BOOK_RE})"
    rf"\.?\s+"
    rf"(\d{{1,3}})"
    rf"(?::(\d{{1,3}}(?:\s*[-\u2013\u2014]\s*\d{{1,3}})?))?",
    re.IGNORECASE,
)

_BOOK_LOOKUP: list[tuple[re.Pattern, str]] = [
    (re.compile(rf"^(?:{pat})$", re.IGNORECASE), canon)
    for pat, canon in _BOOK_PATTERNS
]


def _normalize_book(raw: str) -> str:
    """Map a raw book match to its canonical SBL abbreviation."""
    clean = raw.strip().rstrip(".")
    for pattern, canonical in _BOOK_LOOKUP:
        if pattern.match(clean):
            return canonical
    return clean


def extract_scripture_refs(text: str) -> list[str]:
    """Extract normalized Bible references from scholarly text.

    Returns a deduplicated list of references like ["Gen 1:1", "Rom 3:23"].
    """
    seen: set[str] = set()
    results: list[str] = []

    for match in _REF_PATTERN.finditer(text):
        book_raw = match.group(1)
        chapter = match.group(2)
        verse = match.group(3)

        canonical_book = _normalize_book(book_raw)
        if verse:
            ref = f"{canonical_book} {chapter}:{verse}"
        else:
            ref = f"{canonical_book} {chapter}"

        if ref not in seen:
            seen.add(ref)
            results.append(ref)

    return results
