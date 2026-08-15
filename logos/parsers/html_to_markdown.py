"""Convert Logos book HTML to clean Markdown text."""

from __future__ import annotations

import re
from html.parser import HTMLParser

_BIBLE_REF_RE = re.compile(r"^bible(?:\+\w+)?\.\d+\.\d+")


class _MarkdownExtractor(HTMLParser):
    """Simple HTML to Markdown converter for Logos book content."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._tag_stack: list[str] = []
        self.bible_refs: set[str] = set()
        self.page_markers: list[dict] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._tag_stack.append(tag)
        attr_dict = dict(attrs)

        if tag in ("p", "div") and self.parts and self.parts[-1] != "\n\n":
            self.parts.append("\n\n")
        elif tag == "br":
            self.parts.append("\n")
        elif tag in ("b", "strong"):
            self.parts.append("**")
        elif tag in ("i", "em"):
            self.parts.append("*")
        elif tag == "sup":
            self.parts.append("^")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(tag[1])
            self.parts.append(f"\n\n{'#' * level} ")
        elif tag == "a":
            data_type = attr_dict.get("data-datatype", "")
            if data_type in ("vp", "page"):
                raw_ref = attr_dict.get("data-raw-reference", "")
                char_pos = sum(len(p) for p in self.parts)
                marker: dict = {"raw_ref": raw_ref, "char_position": char_pos}
                if data_type == "vp":
                    parts = raw_ref.split(".", 2)
                    if len(parts) == 3:
                        marker["volume"] = parts[1]
                        marker["page"] = parts[2]
                else:
                    parts = raw_ref.split(".", 1)
                    if len(parts) == 2:
                        marker["page"] = parts[1]
                self.page_markers.append(marker)
            data_ref = attr_dict.get("data-reference", "")
            if data_ref and _BIBLE_REF_RE.match(data_ref):
                self.bible_refs.add(data_ref)

    def handle_endtag(self, tag: str) -> None:
        if self._tag_stack and self._tag_stack[-1] == tag:
            self._tag_stack.pop()

        if tag in ("b", "strong"):
            self.parts.append("**")
        elif tag in ("i", "em"):
            self.parts.append("*")
        elif tag == "sup":
            self.parts.append("^")
        elif tag in ("p", "div"):
            self.parts.append("\n\n")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def get_markdown(self) -> str:
        text = "".join(self.parts)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r" {2,}", " ", text)
        return text.strip()

    def get_bible_refs(self) -> list[str]:
        return sorted(self.bible_refs)

    def get_page_markers(self) -> list[dict]:
        return list(self.page_markers)


def html_to_markdown(html: str) -> str:
    """Convert HTML string to Markdown text."""
    if not html or not html.strip():
        return ""
    parser = _MarkdownExtractor()
    parser.feed(html)
    return parser.get_markdown()


def html_to_markdown_with_refs(html: str) -> tuple[str, list[str], list[dict]]:
    """Convert HTML to Markdown and return extracted Bible refs and page markers."""
    if not html or not html.strip():
        return "", [], []
    parser = _MarkdownExtractor()
    parser.feed(html)
    return parser.get_markdown(), parser.get_bible_refs(), parser.get_page_markers()
