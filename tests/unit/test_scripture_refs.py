"""Tests for scripture reference extraction."""

from logos.ingest.scripture_refs import extract_scripture_refs


def test_basic_references():
    text = "See Romans 3:23 and Genesis 1:1 for context."
    refs = extract_scripture_refs(text)
    assert "Rom 3:23" in refs
    assert "Gen 1:1" in refs


def test_chapter_only():
    text = "The argument of Romans 8 is central to Paul's theology."
    refs = extract_scripture_refs(text)
    assert "Rom 8" in refs


def test_verse_range():
    text = "Compare John 3:16-18 with Romans 5:1-5."
    refs = extract_scripture_refs(text)
    assert any("John 3:16" in r for r in refs)
    assert any("Rom 5:1" in r for r in refs)


def test_abbreviations():
    text = "Gen 1:1, Exod 3:14, Lev 19:18, Deut 6:4, Matt 5:1"
    refs = extract_scripture_refs(text)
    assert "Gen 1:1" in refs
    assert "Exod 3:14" in refs
    assert "Lev 19:18" in refs
    assert "Deut 6:4" in refs
    assert "Matt 5:1" in refs


def test_full_book_names():
    text = "Genesis 1:1 and Revelation 22:21"
    refs = extract_scripture_refs(text)
    assert "Gen 1:1" in refs
    assert "Rev 22:21" in refs


def test_numbered_books():
    text = "1 Corinthians 13:1 and 2 Timothy 3:16"
    refs = extract_scripture_refs(text)
    assert "1 Cor 13:1" in refs
    assert "2 Tim 3:16" in refs


def test_deduplication():
    text = "Romans 3:23 is cited. Again, Romans 3:23 appears here."
    refs = extract_scripture_refs(text)
    assert refs.count("Rom 3:23") == 1


def test_empty_text():
    assert extract_scripture_refs("") == []


def test_no_refs():
    text = "This text has no Bible references at all."
    assert extract_scripture_refs(text) == []


def test_all_66_books_represented():
    """Smoke test that the pattern covers representative books."""
    texts = [
        "Gen 1:1", "Exod 3:14", "Lev 19:18", "Num 6:24", "Deut 6:4",
        "Josh 1:9", "Judg 6:12", "Ruth 1:16",
        "1 Sam 3:10", "2 Sam 7:12", "1 Kgs 8:27", "2 Kgs 5:14",
        "1 Chr 16:11", "2 Chr 7:14", "Ezra 7:10", "Neh 8:10",
        "Esth 4:14", "Job 1:21", "Ps 23:1", "Prov 3:5",
        "Eccl 3:1", "Song 2:4", "Isa 53:5", "Jer 29:11",
        "Lam 3:22", "Ezek 37:1", "Dan 3:17", "Hos 6:6",
        "Joel 2:28", "Amos 5:24", "Obad 1", "Jonah 2:9",
        "Mic 6:8", "Nah 1:7", "Hab 2:4", "Zeph 3:17",
        "Hag 2:9", "Zech 9:9", "Mal 3:10",
        "Matt 5:1", "Mark 1:1", "Luke 2:10", "John 3:16",
        "Acts 2:38", "Rom 8:28", "1 Cor 13:4", "2 Cor 5:17",
        "Gal 5:22", "Eph 2:8", "Phil 4:13", "Col 3:23",
        "1 Thess 5:16", "2 Thess 3:3", "1 Tim 2:5", "2 Tim 3:16",
        "Titus 3:5", "Phlm 1", "Heb 11:1", "Jas 1:2",
        "1 Pet 5:7", "2 Pet 3:9", "1 John 4:8", "2 John 1",
        "3 John 1", "Jude 1", "Rev 21:4",
    ]
    for ref_text in texts:
        refs = extract_scripture_refs(ref_text)
        assert len(refs) > 0, f"Failed to extract reference from: {ref_text}"
