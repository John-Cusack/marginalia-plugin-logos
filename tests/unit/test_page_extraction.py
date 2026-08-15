"""Tests for page milestone extraction from Logos HTML."""

from logos.parsers.html_to_markdown import html_to_markdown_with_refs


def test_vp_marker_extraction():
    html = (
        '<p>Some text before.</p>'
        '<a data-datatype="vp" data-reference="Volume 1, Page 18"'
        ' data-raw-reference="vp.1.18" rel="milestone"'
        ' style="display: inline-block; height: 1em; width: 0px;"></a>'
        '<p>Text after the marker.</p>'
    )
    md, refs, markers = html_to_markdown_with_refs(html)
    assert len(markers) == 1
    assert markers[0]["raw_ref"] == "vp.1.18"
    assert markers[0]["volume"] == "1"
    assert markers[0]["page"] == "18"
    assert "char_position" in markers[0]


def test_page_marker_extraction():
    html = (
        '<a data-datatype="page" data-reference="Page viii"'
        ' data-raw-reference="page.viii" rel="milestone"'
        ' style="display: inline-block; height: 1em; width: 0px;"></a>'
        '<p>Content on page viii.</p>'
    )
    md, refs, markers = html_to_markdown_with_refs(html)
    assert len(markers) == 1
    assert markers[0]["raw_ref"] == "page.viii"
    assert markers[0]["page"] == "viii"
    assert "volume" not in markers[0]


def test_mixed_markers():
    html = (
        '<a data-datatype="vp" data-reference="Volume 1, Page 18"'
        ' data-raw-reference="vp.1.18" rel="milestone"'
        ' style="display: inline-block; height: 1em; width: 0px;"></a>'
        '<p>First page content.</p>'
        '<a data-datatype="page" data-reference="Page ix"'
        ' data-raw-reference="page.ix" rel="milestone"'
        ' style="display: inline-block; height: 1em; width: 0px;"></a>'
        '<p>Second page content.</p>'
    )
    md, refs, markers = html_to_markdown_with_refs(html)
    assert len(markers) == 2
    assert markers[0]["raw_ref"] == "vp.1.18"
    assert markers[1]["raw_ref"] == "page.ix"


def test_no_markers():
    html = '<p>Just some plain text.</p>'
    md, refs, markers = html_to_markdown_with_refs(html)
    assert markers == []


def test_char_position_tracks_text():
    html = (
        '<p>Hello world</p>'
        '<a data-datatype="page" data-reference="Page 5"'
        ' data-raw-reference="page.5" rel="milestone"'
        ' style="display: inline-block; height: 1em; width: 0px;"></a>'
        '<p>After marker</p>'
    )
    md, refs, markers = html_to_markdown_with_refs(html)
    assert len(markers) == 1
    # The char_position should be after "Hello world" + surrounding whitespace
    pos = markers[0]["char_position"]
    assert pos > 0


def test_empty_html_returns_empty_markers():
    md, refs, markers = html_to_markdown_with_refs("")
    assert md == ""
    assert refs == []
    assert markers == []


def test_roman_numeral_page():
    html = (
        '<a data-datatype="page" data-reference="Page vii"'
        ' data-raw-reference="page.vii" rel="milestone"'
        ' style="display: inline-block; height: 1em; width: 0px;"></a>'
        '<p>Front matter.</p>'
    )
    md, refs, markers = html_to_markdown_with_refs(html)
    assert markers[0]["page"] == "vii"


def test_bible_refs_still_extracted_alongside_page_markers():
    html = (
        '<a data-datatype="vp" data-reference="Volume 1, Page 18"'
        ' data-raw-reference="vp.1.18" rel="milestone"'
        ' style="display: inline-block; height: 1em; width: 0px;"></a>'
        '<p>See <a data-reference="bible.1.3.6">Genesis 3:6</a>.</p>'
    )
    md, refs, markers = html_to_markdown_with_refs(html)
    assert len(markers) == 1
    assert "bible.1.3.6" in refs
