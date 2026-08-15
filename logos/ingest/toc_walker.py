"""Walk a Logos TOC tree, extracting author attribution and heading hierarchy."""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
from typing import Any

_AUTHOR_RE = re.compile(
    r"\s*\("
    r"("
    r"(?:[A-Z]\.?\s+)*"
    r"[A-Z][a-z]+"
    r"(?:\s+[A-Z]\.)*"
    r"(?:\s+[A-Z][a-z]+)+"
    r")"
    r"\)\s*$"
)

_OFFSET_RE = re.compile(r'data-offset="(\d+)"')


@dataclass
class TocNode:
    """A single TOC entry with extracted metadata."""

    id: str
    title: str
    author: str | None = None
    heading_path: list[str] = field(default_factory=list)
    has_children: bool = False
    offset: int = 0
    length: int = 0


def parse_author(title: str) -> tuple[str, str | None]:
    """Extract a trailing parenthesized author name from a TOC title."""
    m = _AUTHOR_RE.search(title)
    if m:
        clean = title[: m.start()].strip()
        return clean, m.group(1)
    return title, None


def walk_toc(
    toc_data: Any,
    parent_path: list[str] | None = None,
    parent_author: str | None = None,
) -> list[TocNode]:
    """Recursively walk a Logos TOC structure, returning flat TocNode list."""
    if parent_path is None:
        parent_path = []

    items = _extract_items(toc_data)
    nodes: list[TocNode] = []

    for item in items:
        if not isinstance(item, dict):
            continue

        raw_title = item.get("title", "")
        entry_id = item.get("id", "")
        clean_title, author = parse_author(raw_title)

        effective_author = author or parent_author
        current_path = parent_path + [clean_title] if clean_title else list(parent_path)

        children_items = _get_children(item)
        has_children = len(children_items) > 0

        nodes.append(
            TocNode(
                id=entry_id,
                title=clean_title,
                author=effective_author,
                heading_path=current_path,
                has_children=has_children,
                offset=item.get("indexedOffset", 0),
                length=item.get("indexedLength", 0),
            )
        )

        if children_items:
            child_nodes = walk_toc(
                children_items,
                parent_path=current_path,
                parent_author=effective_author,
            )
            nodes.extend(child_nodes)

    return nodes


class TocOffsetIndex:
    """Map article HTML offsets to TOC nodes using binary search."""

    def __init__(self, nodes: list[TocNode]) -> None:
        sorted_nodes = sorted(
            (n for n in nodes if n.offset > 0), key=lambda n: n.offset
        )
        self._offsets = [n.offset for n in sorted_nodes]
        self._nodes = sorted_nodes

    def lookup(self, html: str) -> TocNode | None:
        """Find the TOC node for an article by extracting its first data-offset."""
        m = _OFFSET_RE.search(html)
        if not m:
            return None
        offset = int(m.group(1))
        return self._lookup_offset(offset)

    def _lookup_offset(self, offset: int) -> TocNode | None:
        if not self._offsets:
            return None
        idx = bisect.bisect_right(self._offsets, offset) - 1
        if idx < 0:
            return None
        return self._nodes[idx]


def extract_first_offset(html: str) -> int | None:
    """Extract the first data-offset value from article HTML."""
    m = _OFFSET_RE.search(html)
    return int(m.group(1)) if m else None


def _extract_items(data: Any) -> list[dict]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "children", "entries"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def _get_children(item: dict) -> list[dict]:
    for key in ("children", "items"):
        children = item.get(key)
        if isinstance(children, list) and children:
            return children
    return []
