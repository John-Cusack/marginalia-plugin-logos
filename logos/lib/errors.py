"""Recognising an embedding outage across the SDK boundary.

`research_engine_sdk.EmbeddingUnavailable` is what the SDK documents for "the
configured embedding backend cannot currently serve ingestion", and it is the
only such class a plugin can import. Research Engine 0.6 does not raise it:
`research_engine.domain.errors.EmbeddingUnavailable` is a separate class in
core's own hierarchy — only `PermissionDenied` is re-exported from the SDK — so
a plain `except EmbeddingUnavailable` here never fires against a real engine.

That matters because of what the plugin does with it. An outage must stop the
store, not halve the batch: a real run split 50 passages into 25, 12, 6, 6, 13,
6, 7 against a host that had been switched off for three days, sixteen calls to
the same dead server. Treating the outage as an ordinary failure brings that
back, so the class is matched by name as well — narrowly, and only for classes
defined in the engine.

Once core's `EmbeddingUnavailable` derives from the SDK's, this becomes
`isinstance` and the module can go.
"""

from __future__ import annotations

from research_engine_sdk import EmbeddingUnavailable


def is_embedding_unavailable(exc: BaseException) -> bool:
    """True for the SDK's error, or the engine's own class of the same name."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, EmbeddingUnavailable):
            return True
        kind = type(current)
        if kind.__name__ == "EmbeddingUnavailable" and (
            kind.__module__ == "research_engine"
            or kind.__module__.startswith("research_engine.")
        ):
            return True
        current = current.__cause__ or current.__context__
    return False
