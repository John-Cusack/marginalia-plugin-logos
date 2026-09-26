"""Regression tests for logos.passage_text argument propagation.

The defective handler posted ``{"passages": [{"passageId": ...}], "resourceIds":
[...]}`` to comparisonV2/verses. The API silently ignores those fields and
returns the user's saved default panel (John 1, ESV/LEB/KJV/NIV), so every
reference collapsed to the same passage and ``versions`` never took effect.
These tests pin the request shape the web client actually sends
(``strReference``/``rawReference`` + ``resourceNames``) with at least two
distinct references, and fail against the old body.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import logos.tools.passage_text as passage_text
from logos.tools.passage_text import handler


def _canned(payload_input: str, resource_ids: list[str]) -> dict:
    return {
        "referenceInput": payload_input,
        "reference": {"raw": f"bible.{payload_input}"},
        "comparisons": [
            {
                "kind": "verseContent",
                "verseContent": {
                    "reference": {"raw": "bible+x", "rendered": payload_input},
                    "resources": [
                        {
                            "resourceId": rid,
                            "text": f"{payload_input} text ({rid})",
                        }
                        for rid in resource_ids
                    ],
                },
            }
        ],
    }


async def _run(reference: str, versions: list[str] | None = None) -> tuple[dict, dict]:
    """Invoke the handler with a stubbed HTTP layer; return (body, payload)."""
    seen: dict = {}

    async def fake_post(path: str, body: dict | None = None) -> dict:
        seen["path"] = path
        seen["body"] = body or {}
        names = seen["body"].get("resourceNames") or ["leb"]
        label = (
            seen["body"].get("strReference")
            or seen["body"].get("rawReference")
            or "John 1"
        )
        ids = [f"LLS:{n.upper()}" for n in names]
        return _canned(label, ids)

    with patch.object(passage_text.logos_client, "post", side_effect=fake_post):
        raw = await handler(reference=reference, versions=versions)
    return seen["body"], json.loads(raw)


async def test_natural_reference_uses_str_reference_and_single_version():
    body, payload = await _run("Luke 3:12-14", ["ESV"])
    assert body == {"strReference": "Luke 3:12-14", "resourceNames": ["esv"]}
    assert "passages" not in body and "resourceIds" not in body
    assert payload["referenceInput"] == "Luke 3:12-14"
    resources = payload["comparisons"][0]["verseContent"]["resources"]
    assert [r["resourceId"] for r in resources] == ["LLS:ESV"]


async def test_two_distinct_references_cannot_return_same_passage():
    _, luke = await _run("Luke 3:12-14", ["ESV"])
    _, daniel = await _run("Daniel 4:27", ["ESV"])
    assert luke["referenceInput"] == "Luke 3:12-14"
    assert daniel["referenceInput"] == "Daniel 4:27"
    assert luke["referenceInput"] != daniel["referenceInput"]
    assert "John 1" not in (luke["referenceInput"], daniel["referenceInput"])


async def test_explicit_esv_does_not_expand_to_comparison_set():
    body, payload = await _run("Luke 3:12-14", ["ESV"])
    assert body["resourceNames"] == ["esv"]
    resources = payload["comparisons"][0]["verseContent"]["resources"]
    assert len(resources) == 1


async def test_version_aliases_map_to_library_resource_names():
    from logos.tools.passage_text import _resource_name

    assert _resource_name("ESV") == "esv"
    assert _resource_name("LEB") == "leb"
    assert _resource_name("NIV") == "niv2011"
    assert _resource_name("KJV") == "kjv1900"
    assert _resource_name("NASB") == "nasb95"


async def test_logos_format_normalizes_to_short_raw_reference():
    from logos.tools.passage_text import _comparison_body

    assert _comparison_body("bible.63.3.12-bible.63.3.14", ["ESV"]) == {
        "rawReference": "bible.63.3.12-63.3.14",
        "resourceNames": ["esv"],
    }
    assert _comparison_body("bible.63.3.12-63.3.14", ["ESV"])["rawReference"] == (
        "bible.63.3.12-63.3.14"
    )


async def test_same_verse_range_collapses_to_single_reference():
    from logos.tools.passage_text import _comparison_body

    # The API 400s the degenerate bible.A-A range (verified live 2026-09-26).
    assert (
        _comparison_body("bible.62.3.16-bible.62.3.16", ["ESV"])["rawReference"]
        == "bible.62.3.16"
    )


async def test_qualified_range_second_half_is_stripped():
    from logos.tools.passage_text import _comparison_body

    assert (
        _comparison_body("bible+esv.66.13.3-bible+esv.66.13.5", ["ESV"])["rawReference"]
        == "bible.66.13.3-66.13.5"
    )
    assert (
        _comparison_body("bible+esv.66.13.3", ["ESV"])["rawReference"]
        == "bible.66.13.3"
    )


async def test_mixed_lls_and_names_rejected_loudly():
    import pytest

    from logos.tools.passage_text import _comparison_body

    with pytest.raises(ValueError, match="not mixed"):
        _comparison_body("Luke 3:12-14", ["LLS:1.0.710", "ESV"])


async def test_api_errors_propagate_instead_of_default_panel():
    import pytest

    with patch.object(
        passage_text.logos_client, "post", side_effect=RuntimeError("400 Bad Request")
    ):
        with pytest.raises(RuntimeError, match="400"):
            await handler(reference="Luke 3:12-14", versions=["ESV"])


async def test_documented_example_round_trips_through_body_builder():
    import re

    from logos.tools.passage_text import _comparison_body

    description = handler._tool_input_schema["properties"]["reference"]["description"]
    for example in re.findall(r"bible\.[0-9.\-]+", description):
        body = _comparison_body(example, ["ESV"])
        assert "-bible." not in body["rawReference"]
        assert body["rawReference"].count("-") <= 1


async def test_manifest_description_matches_code_schema():
    from pathlib import Path

    import yaml

    manifest = yaml.safe_load(
        (Path(__file__).parents[2] / "logos" / "plugin.yaml").read_text()
    )
    (spec,) = [
        t for t in manifest["provides"]["mcp_tools"] if t["id"] == "logos.passage_text"
    ]
    assert (
        spec["input_schema"]["properties"]["reference"]["description"]
        == handler._tool_input_schema["properties"]["reference"]["description"]
    )


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("ESV", "esv"),
        ("esv", "esv"),
        ("LEB", "leb"),
        ("NIV", "niv2011"),
        ("KJV", "kjv1900"),
        ("NASB", "nasb95"),
        ("NASB95", "nasb95"),
        ("NASB2020", "nasb2020"),
        ("NLT", "nlt"),
        ("ASV", "asv"),
        ("  ESV  ", "esv"),
        ("LLS:1.0.710", "LLS:1.0.710"),
    ],
)
async def test_version_matrix(version: str, expected: str):
    from logos.tools.passage_text import _resource_name

    assert _resource_name(version) == expected


async def test_default_version_is_leb():
    body, _ = await _run("Luke 3:12-14", None)
    assert body["resourceNames"] == ["leb"]
