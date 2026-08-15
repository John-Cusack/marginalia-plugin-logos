"""FilterExtension for scripture reference range queries."""

from __future__ import annotations

import re
from typing import Any

import sqlalchemy as sa


# Book order (1-indexed) matching Protestant canon.
_BOOK_ORDER: dict[str, int] = {
    "Gen": 1, "Exod": 2, "Lev": 3, "Num": 4, "Deut": 5,
    "Josh": 6, "Judg": 7, "Ruth": 8, "1 Sam": 9, "2 Sam": 10,
    "1 Kgs": 11, "2 Kgs": 12, "1 Chr": 13, "2 Chr": 14,
    "Ezra": 15, "Neh": 16, "Esth": 17, "Job": 18, "Ps": 19,
    "Prov": 20, "Eccl": 21, "Song": 22, "Isa": 23, "Jer": 24,
    "Lam": 25, "Ezek": 26, "Dan": 27, "Hos": 28, "Joel": 29,
    "Amos": 30, "Obad": 31, "Jonah": 32, "Mic": 33, "Nah": 34,
    "Hab": 35, "Zeph": 36, "Hag": 37, "Zech": 38, "Mal": 39,
    "Matt": 40, "Mark": 41, "Luke": 42, "John": 43, "Acts": 44,
    "Rom": 45, "1 Cor": 46, "2 Cor": 47, "Gal": 48, "Eph": 49,
    "Phil": 50, "Col": 51, "1 Thess": 52, "2 Thess": 53,
    "1 Tim": 54, "2 Tim": 55, "Titus": 56, "Phlm": 57, "Heb": 58,
    "Jas": 59, "1 Pet": 60, "2 Pet": 61, "1 John": 62,
    "2 John": 63, "3 John": 64, "Jude": 65, "Rev": 66,
}

_REF_RE = re.compile(r"^(.+?)\s+(\d+)(?::(\d+))?")


def ref_to_ordinal(ref: str) -> int:
    """Convert a scripture reference string to a sortable integer ordinal.

    Encoding: book * 1_000_000 + chapter * 1_000 + verse
    Chapter-only refs use verse=0.  Book-only refs use chapter=0, verse=0.

    >>> ref_to_ordinal("Gen 1:1")
    1001001
    >>> ref_to_ordinal("Rom 3:23")
    45003023
    >>> ref_to_ordinal("Rev 22")
    66022000
    """
    m = _REF_RE.match(ref.strip())
    if not m:
        book_num = _BOOK_ORDER.get(ref.strip(), 0)
        return book_num * 1_000_000

    book_str = m.group(1).strip()
    chapter = int(m.group(2))
    verse = int(m.group(3)) if m.group(3) else 0

    book_num = _BOOK_ORDER.get(book_str, 0)
    return book_num * 1_000_000 + chapter * 1_000 + verse


def ordinal_to_ref(ordinal: int) -> str:
    """Convert an ordinal back to a human-readable reference.

    >>> ordinal_to_ref(1001001)
    'Gen 1:1'
    >>> ordinal_to_ref(66022000)
    'Rev 22'
    """
    book_num = ordinal // 1_000_000
    remainder = ordinal % 1_000_000
    chapter = remainder // 1_000
    verse = remainder % 1_000

    book_name = next(
        (k for k, v in _BOOK_ORDER.items() if v == book_num),
        f"Book{book_num}",
    )
    if verse > 0:
        return f"{book_name} {chapter}:{verse}"
    elif chapter > 0:
        return f"{book_name} {chapter}"
    else:
        return book_name


def _build_book_patterns(start_ord: int, end_ord: int) -> list[str]:
    """Build SQL LIKE patterns for refs in the ordinal range.

    For a single-book, single-chapter query like "Rom 3", generates::

        ["Rom 3", "Rom 3:%"]

    For a multi-chapter range like "Rom 1" to "Rom 8", generates::

        ["Rom 1", "Rom 1:%", "Rom 2", "Rom 2:%", ..., "Rom 8", "Rom 8:%"]

    For multi-book ranges, intermediate books match broadly ("Book %").
    """
    start_book = start_ord // 1_000_000
    end_book = end_ord // 1_000_000

    patterns: list[str] = []
    for book_num in range(start_book, end_book + 1):
        book_name = next((k for k, v in _BOOK_ORDER.items() if v == book_num), None)
        if not book_name:
            continue

        if book_num == start_book and book_num == end_book:
            # Same book — chapter-level precision
            start_ch = (start_ord % 1_000_000) // 1_000
            end_ch = (end_ord % 1_000_000) // 1_000
            if start_ch == 0 and end_ch == 0:
                patterns.append(f"{book_name} %")
            else:
                for ch in range(max(start_ch, 1), end_ch + 1):
                    patterns.append(f"{book_name} {ch}")
                    patterns.append(f"{book_name} {ch}:%")
        else:
            # First/last/middle books in a multi-book range — match all chapters.
            # Slightly over-matches for edge books but correct for practical use
            # (e.g., searching "Matt through John" should include all of Matthew).
            patterns.append(f"{book_name} %")

    return patterns


class ScriptureRefRangeFilter:
    """Filter passages by scripture reference range.

    Works with existing data where ``metadata.scripture_refs`` is a list
    of strings like ``["Gen 1:1", "Rom 3:23"]``.  Generates SQL LIKE
    patterns for the books/chapters in the range and checks if any
    stored ref matches.

    For better performance at scale, future work could store ordinals
    in metadata during chunking and use integer range comparisons.
    """

    @property
    def filter_id(self) -> str:
        return "scripture_ref_range"

    @property
    def description(self) -> str:
        return (
            "Filter passages by scripture reference range. "
            "Example: start='Gen 1:1', end='Gen 3:24' finds passages "
            "referencing any verse in Genesis 1-3. Uses SBL abbreviations "
            "(Gen, Exod, Lev, Matt, Rom, Rev, etc.)."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "start": {
                    "type": "string",
                    "description": "Start reference (SBL format), e.g. 'Gen 1:1' or 'Rom 1'.",
                },
                "end": {
                    "type": "string",
                    "description": (
                        "End reference (SBL format), e.g. 'Gen 3:24' or 'Rom 8'. "
                        "Defaults to start if omitted (single chapter/verse)."
                    ),
                },
            },
            "required": ["start"],
        }

    def build_clause(self, value: Any) -> sa.sql.expression.SelectBase:
        start_ref = value["start"]
        end_ref = value.get("end", start_ref)

        start_ord = ref_to_ordinal(start_ref)
        end_ord = ref_to_ordinal(end_ref)

        # If end is chapter-only (verse=0), set to max verse in that chapter
        if end_ord % 1_000 == 0 and end_ord > 0:
            end_ord += 999

        patterns = _build_book_patterns(start_ord, end_ord)

        return sa.select(
            sa.literal_column("p.id").label("passage_id")
        ).select_from(
            sa.text("core.passages p")
        ).where(
            sa.text(
                "EXISTS ("
                "  SELECT 1"
                "  FROM jsonb_array_elements_text(p.metadata->'scripture_refs') AS r(ref)"
                "  WHERE r.ref LIKE ANY(:book_patterns)"
                ")"
            ).bindparams(
                sa.bindparam("book_patterns", value=patterns)
            )
        )
