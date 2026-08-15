"""IngestionModule for Logos book articles."""

from __future__ import annotations

from typing import Any, ClassVar

from research_engine.plugins.sdk.interfaces import IngestionModule


class LogosBookModule(IngestionModule):
    """Handles Logos book content fetched via the articles API.

    This module is used for commentary content that comes through
    the Logos API rather than from files on disk.
    """

    id: ClassVar[str] = "logos_book"
    version: ClassVar[str] = "1.0"
    supported_extensions: ClassVar[list[str]] = []
    supported_mime_types: ClassVar[list[str]] = ["application/x-logos-book"]

    async def detect(self, source_path: Any) -> tuple[float, str]:
        # This module is invoked programmatically, not via file detection
        return 0.0, "Logos books are ingested via API, not file detection"

    async def parse(self, source_path: Any) -> tuple[str, str | None, dict[str, Any]]:
        # Parsing is handled by the ingest_book tool which calls the Logos API
        raise NotImplementedError(
            "LogosBookModule.parse() should not be called directly. "
            "Use logos.ingest_book tool instead."
        )

    def default_chunker(self) -> str:
        return "verse_boundary"

    def default_document_type(self) -> str:
        return "logos_book"
