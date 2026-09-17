"""A book's flat section list, nested into the node tree core stores.

`IngestionClient.ingest_drafts` stores `NodeDraft`s as given: an ltree path per
node, parents before children, spans that enclose their descendants. Core no
longer turns a section list into that tree for a plugin, so this does, by the
rule core's own `research_engine.domain.nodes.build_node_tree` applies to its
parsers. Before 0.2.0 the plugin called that function directly; keeping the rule
identical means a book stored by 0.2.0 has exactly the outline 0.1.x gave it.
The integration suite checks the two against each other whenever core is
installed.
"""

from __future__ import annotations

from typing import Any

from research_engine_sdk import NodeDraft

#: Label of the synthetic root spanning the whole canonical text. Every tree has
#: one, so ancestor queries have a uniform terminus.
ROOT_PATH = "r"

_SECTION_KEYS = {"char_start", "char_end", "heading", "level", "node_type"}


def build_node_tree(
    sections: list[dict[str, Any]],
    *,
    text_length: int,
    title: str | None = None,
) -> list[NodeDraft]:
    """Nest *sections* by ``level`` under a root, as SDK node drafts.

    *sections* are in document order, each with ``char_start``/``char_end``, an
    optional ``level`` (default 1) and ``heading``; any other key becomes node
    metadata. A section parents to the nearest preceding section of a shallower
    level, so a level that skips a rank attaches to the closest ancestor there
    is. A section without a span is skipped: it could not be addressed.

    Drafts come back parents first, and each parent's span is widened to enclose
    its children's.
    """
    root = NodeDraft(
        path=ROOT_PATH,
        parent_path=None,
        depth=0,
        position=0,
        node_type="document",
        title=title,
        char_start=0,
        char_end=text_length,
    )
    drafts = [root]
    stack: list[tuple[int, NodeDraft]] = [(0, root)]
    child_counts = {ROOT_PATH: 0}

    for section in sections:
        start, end = section.get("char_start"), section.get("char_end")
        if start is None or end is None:
            continue

        level = section.get("level") or 1
        while len(stack) > 1 and stack[-1][0] >= level:
            stack.pop()
        parent = stack[-1][1]

        position = child_counts[parent.path]
        child_counts[parent.path] = position + 1
        path = f"{parent.path}.n{position}"
        child_counts[path] = 0

        draft = NodeDraft(
            path=path,
            parent_path=parent.path,
            depth=parent.depth + 1,
            position=position,
            node_type=section.get("node_type", "section"),
            title=section.get("heading"),
            char_start=start,
            char_end=end,
            metadata={k: v for k, v in section.items() if k not in _SECTION_KEYS},
        )
        drafts.append(draft)
        stack.append((level, draft))

    # Deepest first, so a child has absorbed its own descendants before its
    # parent reads it.
    by_path = {draft.path: draft for draft in drafts}
    for draft in reversed(drafts):
        parent = by_path.get(draft.parent_path or "")
        if parent is not None:
            parent.char_start = min(parent.char_start, draft.char_start)
            parent.char_end = max(parent.char_end, draft.char_end)
    return drafts
