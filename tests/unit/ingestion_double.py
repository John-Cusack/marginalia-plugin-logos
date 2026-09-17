"""An `IngestionClient` that refuses what core would refuse, and records the rest.

The SDK ships no ingestion double, so this is the plugin's own. It checks the
drafts the way core 0.6 does before storing them: `IngestionServiceAdapter`
rejects a passage whose text is not its slice of `full_text`, and the node
repository inserts `NodeDraft`s as given, resolving each `parent_path` to a node
it has already written. So the tree must arrive whole: root first, parents
before children, paths that follow from their parents, and spans that enclose
their descendants' — the containment `attach_nodes` relies on to put each
passage in its deepest section.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from research_engine_sdk import NodeDraft, PassageDraft


def assert_ingestion_drafts(
    passage_drafts: list[PassageDraft],
    full_text: str | None,
    node_drafts: list[NodeDraft] | None,
) -> None:
    assert full_text is not None, "without full_text the offsets address nothing stored"
    assert all(isinstance(d, PassageDraft) for d in passage_drafts)
    assert [d.position for d in passage_drafts] == list(range(len(passage_drafts)))
    for draft in passage_drafts:
        assert 0 <= draft.char_start <= draft.char_end <= len(full_text)
        assert draft.text == full_text[draft.char_start : draft.char_end], (
            f"passage {draft.position} text does not match its canonical span"
        )

    if not node_drafts:
        return
    assert all(isinstance(n, NodeDraft) for n in node_drafts)
    root, *rest = node_drafts
    assert (root.path, root.parent_path, root.depth) == ("r", None, 0)
    assert (root.char_start, root.char_end) == (0, len(full_text))
    seen: dict[str, NodeDraft] = {root.path: root}
    for node in rest:
        parent = seen.get(node.parent_path or "")
        assert parent is not None, f"{node.path} arrives before its parent"
        assert node.path == f"{parent.path}.n{node.position}"
        assert node.depth == parent.depth + 1
        assert parent.char_start <= node.char_start <= node.char_end <= parent.char_end, (
            f"{node.path} is not inside {parent.path}"
        )
        assert node.path not in seen, f"duplicate path {node.path}"
        seen[node.path] = node


class RecordingIngestionClient:
    """Checks every `ingest_drafts` call, then pretends to store it.

    *fail_with* is raised by the first call, before anything is recorded as
    stored, as core raises `EmbeddingUnavailable` from inside its transaction.
    """

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self._fail_with = fail_with
        self.calls: list[dict[str, Any]] = []
        self.documents: list[dict[str, Any]] = []

    async def ingest_drafts(
        self,
        title: str,
        document_type: str,
        passage_drafts: list[PassageDraft],
        *,
        source: str = "",
        metadata: dict[str, Any] | None = None,
        language: str | None = None,
        full_text: str | None = None,
        node_drafts: list[NodeDraft] | None = None,
    ) -> dict[str, Any]:
        assert_ingestion_drafts(passage_drafts, full_text, node_drafts)
        self.calls.append({
            "title": title, "document_type": document_type,
            "passage_drafts": passage_drafts, "source": source,
            "metadata": metadata, "language": language,
            "full_text": full_text, "node_drafts": node_drafts,
        })
        if self._fail_with is not None:
            error, self._fail_with = self._fail_with, None
            raise error
        document = {
            "document_id": str(uuid4()),
            "passage_count": len(passage_drafts),
            "node_count": len(node_drafts or []),
        }
        self.documents.append(document)
        return document

    async def find_existing(
        self, *, source: str | None = None, source_pattern: str | None = None
    ) -> list[dict[str, Any]]:
        return []
