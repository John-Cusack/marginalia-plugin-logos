"""Tests for ScriptureRefRangeFilter and ordinal utilities."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from logos.filters import (
    ScriptureRefRangeFilter,
    _build_book_patterns,
    ordinal_to_ref,
    ref_to_ordinal,
)


class TestRefToOrdinal:
    def test_genesis_1_1(self):
        assert ref_to_ordinal("Gen 1:1") == 1_001_001

    def test_romans_3_23(self):
        assert ref_to_ordinal("Rom 3:23") == 45_003_023

    def test_revelation_22(self):
        assert ref_to_ordinal("Rev 22") == 66_022_000

    def test_book_only(self):
        assert ref_to_ordinal("Gen") == 1_000_000

    def test_matthew_1_1(self):
        assert ref_to_ordinal("Matt 1:1") == 40_001_001

    def test_psalms(self):
        assert ref_to_ordinal("Ps 23:1") == 19_023_001

    def test_numbered_book(self):
        assert ref_to_ordinal("1 Cor 13:4") == 46_013_004

    def test_unknown_book(self):
        # Unknown book gets book_num=0, but chapter/verse still parse
        assert ref_to_ordinal("Unknown 1:1") == 1001
        # Truly unparseable (no chapter) returns 0
        assert ref_to_ordinal("???") == 0


class TestOrdinalToRef:
    def test_genesis_1_1(self):
        assert ordinal_to_ref(1_001_001) == "Gen 1:1"

    def test_chapter_only(self):
        assert ordinal_to_ref(66_022_000) == "Rev 22"

    def test_book_only(self):
        assert ordinal_to_ref(1_000_000) == "Gen"

    def test_roundtrip(self):
        refs = ["Gen 1:1", "Rom 3:23", "Rev 22", "1 Cor 13:4", "Ps 119:105"]
        for ref in refs:
            assert ordinal_to_ref(ref_to_ordinal(ref)) == ref


class TestBuildBookPatterns:
    def test_single_chapter(self):
        # "Rom 3" to "Rom 3"
        patterns = _build_book_patterns(45_003_000, 45_003_999)
        assert "Rom 3" in patterns
        assert "Rom 3:%" in patterns
        assert len(patterns) == 2

    def test_chapter_range(self):
        # "Rom 1" to "Rom 3"
        patterns = _build_book_patterns(45_001_000, 45_003_999)
        assert "Rom 1" in patterns
        assert "Rom 1:%" in patterns
        assert "Rom 2" in patterns
        assert "Rom 3" in patterns
        assert len(patterns) == 6  # 3 chapters * 2 patterns each

    def test_whole_book(self):
        # "Gen" to "Gen" (book-only)
        patterns = _build_book_patterns(1_000_000, 1_000_000)
        assert patterns == ["Gen %"]

    def test_multi_book(self):
        # "Matt" through "John" (books 40-43)
        patterns = _build_book_patterns(40_000_000, 43_999_999)
        assert "Matt %" in patterns
        assert "Mark %" in patterns
        assert "Luke %" in patterns
        assert "John %" in patterns
        assert len(patterns) == 4


class TestScriptureRefRangeFilter:
    def test_protocol_conformance(self):
        pytest.importorskip("research_engine")
        from research_engine.domain.filter_extension import FilterExtension

        f = ScriptureRefRangeFilter()
        assert isinstance(f, FilterExtension)

    def test_properties(self):
        f = ScriptureRefRangeFilter()
        assert f.filter_id == "scripture_ref_range"
        assert "start" in f.input_schema["properties"]
        assert f.input_schema["required"] == ["start"]
        assert len(f.description) > 0

    def test_build_clause_returns_select(self):
        f = ScriptureRefRangeFilter()
        clause = f.build_clause({"start": "Rom 1", "end": "Rom 8"})
        assert isinstance(clause, sa.sql.expression.SelectBase)

    def test_build_clause_single_ref(self):
        f = ScriptureRefRangeFilter()
        clause = f.build_clause({"start": "Gen 1:1"})
        compiled = str(clause.compile())
        assert "scripture_refs" in compiled
        assert "book_patterns" in compiled

    def test_build_clause_end_defaults_to_start(self):
        f = ScriptureRefRangeFilter()
        clause = f.build_clause({"start": "Rom 3"})
        compiled = str(clause.compile())
        assert "scripture_refs" in compiled
