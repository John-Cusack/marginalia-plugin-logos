"""Tests for TOC walker."""

from logos.ingest.toc_walker import (
    TocOffsetIndex,
    extract_first_offset,
    parse_author,
    walk_toc,
)


def test_parse_author_with_name():
    title, author = parse_author("A Classical Calvinist View (Michael S. Horton)")
    assert title == "A Classical Calvinist View"
    assert author == "Michael S. Horton"


def test_parse_author_with_initials():
    title, author = parse_author("Introduction (J. Matthew Pinson)")
    assert title == "Introduction"
    assert author == "J. Matthew Pinson"


def test_parse_author_no_name():
    title, author = parse_author("Glossary")
    assert title == "Glossary"
    assert author is None


def test_walk_toc_flat_list():
    toc_data = [
        {"id": "ch1", "title": "Chapter 1 (Author One)", "indexedOffset": 100, "indexedLength": 50},
        {"id": "ch2", "title": "Chapter 2 (Author Two)", "indexedOffset": 200, "indexedLength": 50},
    ]
    nodes = walk_toc(toc_data)
    assert len(nodes) == 2
    assert nodes[0].title == "Chapter 1"
    assert nodes[0].author == "Author One"
    assert nodes[1].author == "Author Two"


def test_walk_toc_nested():
    toc_data = [
        {
            "id": "part1",
            "title": "Part One (Senior Author)",
            "indexedOffset": 100,
            "children": [
                {"id": "ch1", "title": "Chapter 1", "indexedOffset": 150},
                {"id": "ch2", "title": "Chapter 2 (Junior Author)", "indexedOffset": 200},
            ],
        },
    ]
    nodes = walk_toc(toc_data)
    assert len(nodes) == 3
    # Part One
    assert nodes[0].author == "Senior Author"
    # Chapter 1 inherits parent author
    assert nodes[1].author == "Senior Author"
    # Chapter 2 has own author
    assert nodes[2].author == "Junior Author"


def test_walk_toc_heading_path():
    toc_data = [
        {
            "id": "p1",
            "title": "Part One",
            "children": [
                {"id": "c1", "title": "Chapter 1"},
            ],
        },
    ]
    nodes = walk_toc(toc_data)
    assert nodes[1].heading_path == ["Part One", "Chapter 1"]


def test_walk_toc_dict_with_items():
    toc_data = {
        "items": [
            {"id": "a", "title": "First"},
            {"id": "b", "title": "Second"},
        ]
    }
    nodes = walk_toc(toc_data)
    assert len(nodes) == 2


def test_offset_index_lookup():
    toc_data = [
        {"id": "ch1", "title": "Chapter 1", "indexedOffset": 100},
        {"id": "ch2", "title": "Chapter 2", "indexedOffset": 500},
        {"id": "ch3", "title": "Chapter 3", "indexedOffset": 1000},
    ]
    nodes = walk_toc(toc_data)
    index = TocOffsetIndex(nodes)

    html = '<div data-offset="250">content</div>'
    node = index.lookup(html)
    assert node is not None
    assert node.title == "Chapter 1"

    html = '<div data-offset="750">content</div>'
    node = index.lookup(html)
    assert node is not None
    assert node.title == "Chapter 2"


def test_offset_index_no_match():
    nodes = walk_toc([])
    index = TocOffsetIndex(nodes)
    assert index.lookup('<div data-offset="100">x</div>') is None


def test_extract_first_offset():
    html = '<div data-offset="42"><p data-offset="100">text</p></div>'
    assert extract_first_offset(html) == 42


def test_extract_first_offset_none():
    assert extract_first_offset("<div>no offset</div>") is None
