"""logos.ingest_pdf — Ingest a PDF through core pipeline with verse-boundary chunking."""

from __future__ import annotations

import json
from pathlib import Path

from research_engine.plugins.sdk import tool


@tool(
    id="logos.ingest_pdf",
    description="Ingest a PDF through the core pipeline with verse-boundary chunking. Scripture references are extracted and stored in passage metadata.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the PDF file to ingest.",
            },
            "document_type": {
                "type": "string",
                "description": 'Document type. Defaults to "logos_book" (verse-boundary chunking).',
                "default": "logos_book",
            },
        },
        "required": ["path"],
    },
)
async def handler(path: str, document_type: str = "logos_book", **kwargs) -> str:
    source = Path(path)
    if not source.exists():
        return json.dumps({"error": f"File not found: {path}"})

    # The core ingestion pipeline will use the verse_boundary chunker
    # because logos_book document type has default_chunker = "verse_boundary"
    # in pack.yaml.
    #
    # This tool requires the ingestion client to be available.
    # For now, return instructions for using the core ingest command.
    return json.dumps({
        "message": f"To ingest {source.name}, use the core ingestion pipeline with document_type='{document_type}'.",
        "path": str(source.resolve()),
        "document_type": document_type,
        "chunker": "verse_boundary",
        "note": "The verse_boundary chunker will split on verse references and extract scripture_refs into passage metadata.",
    })
