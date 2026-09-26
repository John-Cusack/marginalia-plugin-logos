"""logos.passage_text — Get Bible passage text."""

from __future__ import annotations

import json
import re

from research_engine_sdk import tool

from logos.lib.context import binds_context
from logos.http.client import logos_client


# Display abbreviation -> Text Comparison ``resourceNames`` entry.
# ``resources``/``resourceIds`` are not accepted here: the comparisonV2 API
# silently ignores unknown fields and returns the user's saved default panel
# (John 1 with ESV/LEB/KJV/NIV for this account), which is how every
# reference collapsed to the same passage. ``resourceNames`` takes the
# library resource name (``esv``), not the display abbreviation, so the
# bare abbreviations that differ (NIV -> niv2011, KJV -> kjv1900,
# NASB -> nasb95) need this map. Anything unlisted falls through lowercased.
_VERSION_TO_RESOURCE_NAME = {
    "ESV": "esv",
    "LEB": "leb",
    "NIV": "niv2011",
    "KJV": "kjv1900",
    "NASB": "nasb95",
    "NASB95": "nasb95",
    "NASB2020": "nasb2020",
    "NLT": "nlt",
}


def _resource_name(version: str) -> str:
    """Map a user ``versions`` entry to a comparisonV2 ``resourceNames`` value."""
    text = version.strip()
    if text.upper().startswith("LLS:"):
        return text
    return _VERSION_TO_RESOURCE_NAME.get(text.upper(), text.lower())


def _comparison_body(reference: str, versions: list[str]) -> dict:
    """Build the comparisonV2/verses body the web client actually sends.

    Natural-language references go in ``strReference``; ``bible.`` references
    go in ``rawReference`` in short-range form (``bible.A-B``, not the
    documented ``bible.A-bible.B`` which the API rejects with 400).
    """
    ref = reference.strip()
    match = re.search(r"bible\+[^.]*\.(.*)", ref)
    if match:
        ref = "bible." + match.group(1)
    body: dict = {}
    if ref.startswith("bible."):
        body["rawReference"] = ref.replace("-bible.", "-")
    else:
        body["strReference"] = ref
    ids = [v for v in (v.strip() for v in versions) if v]
    if ids and all(v.upper().startswith("LLS:") for v in ids):
        body["baseResource"] = ids[0]
        body["resources"] = ",".join(ids)
    else:
        body["resourceNames"] = [_resource_name(v) for v in ids]
    return body


@tool(
    id="logos.passage_text",
    description="Get Bible passage text in one or more translations. Returns verse-by-verse text.",
    input_schema={
        "type": "object",
        "properties": {
            "reference": {
                "type": "string",
                "description": 'Bible reference in Logos format (e.g., "bible.62.3.16-bible.62.3.16") or natural language (e.g., "John 3:16").',
            },
            "versions": {
                "type": "array",
                "items": {"type": "string"},
                "description": 'Bible version abbreviations (e.g., ["ESV", "NASB", "LEB"]). Defaults to ["LEB"].',
            },
        },
        "required": ["reference"],
    },
)
@binds_context
async def handler(reference: str, versions: list[str] | None = None, **kwargs) -> str:
    resolved = versions or ["LEB"]
    body = _comparison_body(reference, resolved)
    data = await logos_client.post(
        "/api/app/tools/text-comparison/comparisonV2/verses", body
    )
    return json.dumps(data, indent=2)
