"""Article ids carry the hierarchy the TOC does not.

Logos writes article ids as a dotted path — `LET.X.1.B.1` is five levels down —
and the walk's TOC stops at letters: every `heading_path` staged across 232,782
articles is one element long. So the ids are the only record of a book's real
structure, and `_book_sections` used to discard them, assigning `level = 2` to
every article whatever its id said. TDNT is 7,982 articles with a hierarchy seven
deep, stored two deep.
"""

from __future__ import annotations

import pytest
from research_engine.domain.nodes import build_node_tree

from logos.tools.ingest_book import _article_level, _assemble_book, _book_sections


def _book(ids: list[str]) -> tuple[list[tuple[str, str]], dict, str]:
    """A book whose articles are just long enough to be distinguishable."""
    articles = [(aid, f"{aid} heading line\n\nBody of {aid}. " * 3) for aid in ids]
    text, spans = _assemble_book(articles)
    return articles, spans, text


class TestLevelFromId:
    def test_depth_comes_from_the_dotted_path(self):
        assert _article_level("LET", under_heading=True) == 2
        assert _article_level("LET.X", under_heading=True) == 3
        assert _article_level("LET.X.1.B.1", under_heading=True) == 6

    def test_without_a_heading_the_article_sits_one_level_higher(self):
        """Nothing to nest under, so the article takes the top level itself."""
        assert _article_level("LET.X", under_heading=False) == 2
        assert _article_level("LET", under_heading=False) == 1


class TestTdntShape:
    IDS = ["LET.X", "LET.X.1", "LET.X.1.A", "LET.X.1.A.1", "LET.X.1.B"]

    def test_a_seven_deep_id_path_produces_a_nested_tree(self):
        articles, spans, text = self._built()
        nodes = build_node_tree(
            self._sections(articles, spans), text_length=len(text), title="TDNT"
        )

        assert max(n.depth for n in nodes) > 2, "still flattened to the old ceiling"

    def test_each_article_parents_to_the_one_above_it(self):
        articles, spans, text = self._built()
        nodes = build_node_tree(
            self._sections(articles, spans), text_length=len(text), title="TDNT"
        )
        by_title = {n.title: n for n in nodes if n.title}

        child = by_title["LET.X.1.A.1 heading line"]
        parent = by_title["LET.X.1.A heading line"]
        assert child.depth == parent.depth + 1
        assert child.path.startswith(parent.path + ".")

    def test_a_container_span_widens_to_cover_its_children(self):
        """A heading-only article has a small span of its own.

        Its children follow it in the text, because the walk follows
        `nextArticleId` and reading order agrees with the id hierarchy. So
        `build_node_tree` widening parents over their children is enough — the
        section table needs no pre-computed parent spans.
        """
        articles, spans, text = self._built()
        nodes = build_node_tree(
            self._sections(articles, spans), text_length=len(text), title="TDNT"
        )
        parent = next(n for n in nodes if n.title == "LET.X.1.A heading line")

        assert parent.char_end >= spans["LET.X.1.A.1"][1]

    def _built(self):
        return _book(self.IDS)

    def _sections(self, articles, spans):
        return _book_sections(articles, spans, dict.fromkeys(self.IDS, "Ξ"))


class TestDegenerateIds:
    def test_an_id_whose_parent_was_never_walked_attaches_to_an_ancestor(self):
        """Between 2% and 20% of ids have no parent article, by resource.

        `build_node_tree` nests with a stack, so a level-6 section with no
        level-5 before it lands on the nearest shallower ancestor. Synthesising
        parents for these would invent nodes with no text and no span.
        """
        ids = ["A", "A.B", "A.B.D.1"]  # A.B.D never walked
        articles, spans, text = _book(ids)
        sections = _book_sections(articles, spans, dict.fromkeys(ids, "Letter"))

        nodes = build_node_tree(sections, text_length=len(text), title="Book")

        orphan = next(n for n in nodes if n.title == "A.B.D.1 heading line")
        parent = next(n for n in nodes if n.title == "A.B heading line")
        assert orphan.path.startswith(parent.path + ".")

    def test_a_flat_resource_is_unchanged(self):
        """BDAG's ids are almost all two deep. There is no hierarchy to recover
        and inventing one would be worse than reporting none."""
        ids = ["ABBR.1", "ABBR.2", "ABBR.3"]
        articles, spans, text = _book(ids)
        sections = _book_sections(articles, spans, dict.fromkeys(ids, "A"))

        levels = [s["level"] for s in sections if s.get("article_id")]
        assert levels == [3, 3, 3]

    def test_a_single_component_id_still_nests_under_its_heading(self):
        ids = ["ALEPH", "BETH"]
        articles, spans, text = _book(ids)
        sections = _book_sections(articles, spans, dict.fromkeys(ids, "Letters"))

        assert [s["level"] for s in sections if s.get("article_id")] == [2, 2]


def test_document_order_is_walk_order_not_id_order():
    """The walk follows `nextArticleId`, and `_assemble_book` lays articles out
    in that order. Ids that sort differently must still nest by position."""
    ids = ["LET.B", "LET.B.10", "LET.B.9"]  # 10 walks before 9
    articles, spans, text = _book(ids)
    sections = _book_sections(articles, spans, dict.fromkeys(ids, "Β"))

    entries = [s for s in sections if s.get("article_id")]
    assert [e["article_id"] for e in entries] == ids
    assert [e["char_start"] for e in entries] == sorted(
        e["char_start"] for e in entries
    )


@pytest.mark.parametrize(
    "ids",
    [
        ["A", "A.1", "A.1.1"],
        ["A.1.1", "A.1", "A"],
        ["X.9.9.9.9.9", "X"],
    ],
)
def test_every_section_still_slices_back_to_its_article(ids):
    """Whatever the id shape, the spans must keep addressing real text."""
    articles, spans, text = _book(ids)
    sections = _book_sections(articles, spans, dict.fromkeys(ids, "H"))

    for section in sections:
        if aid := section.get("article_id"):
            body = dict(articles)[aid]
            assert text[section["char_start"] : section["char_end"]] == body
