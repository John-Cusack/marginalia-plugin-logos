"""Tests for logos.get_entry — verbatim lexicon-entry retrieval.

Hand-built fakes throughout (no network): a FakeClient stands in for
``logos.http.client.logos_client`` and serves canned book/TOC/article/
word-study payloads. Mirrors the tests/unit/test_toc_walker.py style.
"""

from __future__ import annotations

import asyncio
import ast
import json
from pathlib import Path
import time
from urllib.parse import quote

import httpx
import pytest
import yaml

import logos.tools.get_entry as get_entry


@pytest.fixture(autouse=True)
def _clean_caches():
    get_entry._alpha_section_cache.clear()
    yield
    get_entry._alpha_section_cache.clear()


def _http_404(path: str) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", f"https://app.logos.com{path}")
    return httpx.HTTPStatusError(
        "not found", request=req, response=httpx.Response(404, request=req)
    )


class FakeClient:
    """Route-table stand-in for logos_client. Unknown paths 404."""

    def __init__(self, routes: dict[str, object]):
        self.routes = routes
        self.requested: list[str] = []

    async def get(self, path: str):
        self.requested.append(path)
        if path not in self.routes:
            raise _http_404(path)
        payload = self.routes[path]
        if isinstance(payload, Exception):
            raise payload
        return payload


def _span(headword: str, lang: str = "he") -> str:
    return (
        f'<span id="hw1" rel="headword" data-headword="{headword}" '
        f'data-headword-language="{lang}"></span>'
    )


def _env(aid, off, length, content, nxt=None):
    return {
        "article": {
            "content": content,
            "indexedOffset": off,
            "indexedLength": length,
            "articleId": aid,
        },
        "previousArticleId": None,
        "nextArticleId": nxt,
        "resourceTitle": "Fake Lexicon",
    }


def _book(title="Fake Lexicon", hint="TEST.900.0", length=999999):
    return {
        "article": {"articleId": hint},
        "previousArticleId": "TEST.899.0",
        "nextArticleId": "TEST.900.1",
        "resourceTitle": title,
        "resourceLength": length,
    }


def _toc(*nodes):
    return [
        {"id": f"1~{off}", "title": title, "indexedOffset": off, "indexedLength": 10}
        for title, off in nodes
    ]


def _word_guide(ref, links):
    return {
        "guideInfo": {"reference": ref},
        "sections": [
            {
                "kind": "definition",
                "content": {
                    "definition": {
                        "lemmaDetails": {
                            "lemmaLinkInformation": {
                                "lemmaLinks": [
                                    {
                                        "resourceId": rid,
                                        "title": title,
                                        "gloss": gloss,
                                        "indexedOffset": off,
                                        "indexedLength": length,
                                    }
                                    for rid, title, gloss, off, length in links
                                ],
                                "nextToken": None,
                            }
                        }
                    }
                },
            }
        ],
    }


_EMPTY_GUIDE = {"guideInfo": {"reference": None}, "sections": []}


def _use_fake(monkeypatch, routes):
    fake = FakeClient(routes)
    monkeypatch.setattr(get_entry, "logos_client", fake)
    return fake


def _article_paths(fake):
    return [p for p in fake.requested if "/articles/" in p]


# ── Matching tiers ────────────────────────────────────────────────────────────


def test_consonantal_match_unpointed_to_pointed():
    assert get_entry._match_tier("שַׁעַר", "שער") == "consonantal"
    assert get_entry._match_tier("שְׂעַר", "שער") == "consonantal"


def test_exact_and_normalized_tiers():
    assert get_entry._match_tier("λόγος", "λόγος") == "exact"
    # Same visual string, different combining-char order → NFC tier.
    decomposed = "é"  # e + U+0301
    assert get_entry._match_tier(decomposed, "é") == "normalized"
    assert get_entry._match_tier("שַׁעַר", "שָׁלוֹם") is None


# ── Single exact match → verbatim entry ───────────────────────────────────────


async def test_single_match_returns_verbatim_entry(monkeypatch):
    body = "  judgment;—abs.\nמִשְׁפָּט\tEx 21:31  "  # whitespace is sacred
    routes = {
        "/api/app/books/TESTRES": _book("Fake BDB"),
        "/api/app/books/TESTRES/tableofcontents": _toc(
            ("Title Page", 100), ("מ", 4000), ("נ", 6000)
        ),
        "/api/app/guides/word?reference=hw": _word_guide(
            "lemma.lbs.he.hw", [("TESTRES", "BDB", "judgment", 5000, 100)]
        ),
        "/api/app/books/TESTRES/articles/TEST.1.0": _env(
            "TEST.1.0", 100, 50, "<div>front</div>", "TEST.2.0"
        ),
        "/api/app/books/TESTRES/articles/TEST.2.0": _env(
            "TEST.2.0", 4000, 500, _span("מָקוֹם") + "<div>other</div>", "TEST.2.1"
        ),
        "/api/app/books/TESTRES/articles/TEST.2.1": _env(
            "TEST.2.1", 5000, 100, _span("hw") + f"<div>{body}</div>", None
        ),
    }
    _use_fake(monkeypatch, routes)

    raw = await get_entry.handler(resource_id="TESTRES", headword="hw")
    data = json.loads(raw)

    assert data["resource_title"] == "Fake BDB"
    assert data["article_id"] == "TEST.2.1"
    assert data["indexed_offset"] == 5000
    assert data["indexed_length"] == 100
    assert body in data["text"]  # byte-verbatim, no normalization
    assert data["truncated"] is False
    assert data["continuation"] is None
    assert data["status"] == "entry"
    assert data["match"] == "exact"
    assert data["scan_complete"] is True


async def test_toc_ids_never_dereferenced_as_articles(monkeypatch):
    """Letter sections 404 as direct articles — the tool must never fetch
    a ``1~offset`` TOC id through the articles endpoint."""
    routes = {
        "/api/app/books/TESTRES": _book(),
        "/api/app/books/TESTRES/tableofcontents": _toc(
            ("Title Page", 100), ("מ", 4000), ("נ", 6000)
        ),
        "/api/app/guides/word?reference=hw": _EMPTY_GUIDE,
        "/api/app/books/TESTRES/articles/TEST.1.0": _env(
            "TEST.1.0", 100, 50, "<div>front</div>", None
        ),
    }
    fake = _use_fake(monkeypatch, routes)
    await get_entry.handler(resource_id="TESTRES", headword="hw")
    for path in _article_paths(fake):
        aid = path.rsplit("/articles/", 1)[1]
        assert "~" not in aid, f"dereferenced TOC id as article: {aid}"


# ── Ambiguity → candidates, never first-hit ───────────────────────────────────


async def test_ambiguous_headword_returns_gate_and_hair(monkeypatch):
    gate = _span("שַׁעַר") + "<div>I שַׁעַר gate, door; entrance</div>"
    hair = _span("שְׂעַר", "arc") + "<div>II שְׂעַר hair (Aramaic)</div>"
    routes = {
        "/api/app/books/AMB": _book("Fake BDB", hint="AMB.9.0"),
        "/api/app/books/AMB/tableofcontents": _toc(
            ("Title Page", 100), ("ש", 1000), ("ת", 9000)
        ),
        # Word study resolves unpointed שער to the Aramaic hair lemma only
        # (the handler URL-quotes the headword for this path).
        "/api/app/guides/word?reference=%D7%A9%D7%A2%D7%A8": _word_guide(
            "lemma.lbs.arc.שְׂעַר", [("AMB", "BDB", "hair", 2000, 60)]
        ),
        "/api/app/books/AMB/articles/AMB.1.0": _env(
            "AMB.1.0", 100, 50, "<div>front</div>", "AMB.2.0"
        ),
        "/api/app/books/AMB/articles/AMB.2.0": _env(
            "AMB.2.0", 1000, 400, gate, "AMB.2.1"
        ),
        "/api/app/books/AMB/articles/AMB.2.1": _env("AMB.2.1", 2000, 60, hair, None),
    }
    _use_fake(monkeypatch, routes)

    raw = await get_entry.handler(resource_id="AMB", headword="שער")
    data = json.loads(raw)

    assert data.get("ambiguous") is True
    assert data["status"] == "ambiguous"
    assert "text" not in data
    cands = data["candidates"]
    assert len(cands) == 2
    by_hw = {c["headword"]: c for c in cands}
    assert set(by_hw) == {"שַׁעַר", "שְׂעַר"}
    # The word-study gloss rides along on the endorsed (hair) candidate.
    assert by_hw["שְׂעַר"]["gloss"] == "hair"
    assert "gate" in by_hw["שַׁעַר"]["gloss"]
    assert all(c["article_id"] and c["indexed_offset"] for c in cands)


async def test_homonyms_pointed_returns_both_numbered(monkeypatch):
    first = _span("שַׁעַר") + "<div>I שַׁעַר gate</div>"
    second = _span("שַׁעַר") + "<div>II שַׁעַר something else</div>"
    routes = {
        "/api/app/books/HOM": _book(hint="HOM.9.0"),
        "/api/app/books/HOM/tableofcontents": _toc(
            ("Title Page", 10), ("ש", 1000), ("ת", 9000)
        ),
        f"/api/app/guides/word?reference={quote('שַׁעַר')}": _EMPTY_GUIDE,
        "/api/app/books/HOM/articles/HOM.1.0": _env(
            "HOM.1.0", 10, 5, "<div>front</div>", "HOM.2.0"
        ),
        "/api/app/books/HOM/articles/HOM.2.0": _env(
            "HOM.2.0", 1000, 100, first, "HOM.2.1"
        ),
        "/api/app/books/HOM/articles/HOM.2.1": _env("HOM.2.1", 1200, 100, second, None),
    }
    _use_fake(monkeypatch, routes)
    data = json.loads(await get_entry.handler(resource_id="HOM", headword="שַׁעַר"))
    assert data.get("ambiguous") is True
    assert data["status"] == "ambiguous"
    assert [c["article_id"] for c in data["candidates"]] == ["HOM.2.0", "HOM.2.1"]


# ── Direct article fetch ──────────────────────────────────────────────────────


async def test_direct_article_id_fetch(monkeypatch):
    routes = {
        "/api/app/books/TESTRES": _book(),
        "/api/app/books/TESTRES/tableofcontents": _toc(("Title", 10)),
        "/api/app/books/TESTRES/articles/TEST.2.1": _env(
            "TEST.2.1", 5000, 100, _span("מִשְׁפָּט") + "<div>judgment entry</div>", None
        ),
    }
    _use_fake(monkeypatch, routes)
    data = json.loads(
        await get_entry.handler(resource_id="TESTRES", article_id="TEST.2.1")
    )
    assert data["article_id"] == "TEST.2.1"
    assert data["headword"] == "מִשְׁפָּט"
    assert "judgment entry" in data["text"]
    assert data["status"] == "entry"
    assert data["match"] == "exact"
    assert data["scan_complete"] is True


async def test_direct_article_id_missing_raises(monkeypatch):
    routes = {
        "/api/app/books/TESTRES": _book(),
        "/api/app/books/TESTRES/tableofcontents": _toc(("Title", 10)),
    }
    _use_fake(monkeypatch, routes)
    with pytest.raises(ValueError, match="not found"):
        await get_entry.handler(resource_id="TESTRES", article_id="NOPE")


# ── Size cap ──────────────────────────────────────────────────────────────────


async def test_truncation_flag_and_continuation(monkeypatch):
    big = "x" * (get_entry.MAX_TEXT_CHARS + 50)
    routes = {
        "/api/app/books/TESTRES": _book(),
        "/api/app/books/TESTRES/tableofcontents": _toc(("Title", 10)),
        "/api/app/books/TESTRES/articles/BIG.0": _env("BIG.0", 10, len(big), big, None),
    }
    _use_fake(monkeypatch, routes)
    first = json.loads(
        await get_entry.handler(resource_id="TESTRES", article_id="BIG.0")
    )
    assert first["truncated"] is True
    assert first["status"] == "entry"
    assert first["match"] == "exact"
    assert first["scan_complete"] is True
    assert len(first["text"]) == get_entry.MAX_TEXT_CHARS
    assert first["continuation"] == {
        "article_id": "BIG.0",
        "next_start": get_entry.MAX_TEXT_CHARS,
    }
    second = json.loads(
        await get_entry.handler(
            resource_id="TESTRES", article_id="BIG.0", start=get_entry.MAX_TEXT_CHARS
        )
    )
    assert second["truncated"] is False
    assert second["text"] == "x" * 50
    assert second["continuation"] is None
    assert second["status"] == "entry"


# ── Empty / not-found ─────────────────────────────────────────────────────────


async def test_strongs_reference_returns_found_false(monkeypatch):
    routes = {
        "/api/app/books/TESTRES": _book(),
        "/api/app/books/TESTRES/tableofcontents": _toc(("Title", 10)),
        "/api/app/guides/word?reference=H4941": _EMPTY_GUIDE,
    }
    _use_fake(monkeypatch, routes)
    data = json.loads(await get_entry.handler(resource_id="TESTRES", headword="H4941"))
    assert data["found"] is False
    assert data["status"] == "not_found"
    assert data["candidates"] == []


async def test_unmatched_first_consonant_scans_nothing(monkeypatch):
    """A Strong's-style headword matching no letter section must not fall
    back to walking the supplement sections — fast empty result."""
    routes = {
        "/api/app/books/TESTRES": _book(hint="T.9.0"),
        "/api/app/books/TESTRES/tableofcontents": _toc(
            ("Title", 10), ("א", 1000), ("ת", 9000), ("Supplement", 9500)
        ),
        "/api/app/guides/word?reference=H4941": _EMPTY_GUIDE,
        "/api/app/books/TESTRES/articles/T.1.0": _env(
            "T.1.0", 10, 5, "<div>front</div>", None
        ),
    }
    fake = _use_fake(monkeypatch, routes)
    data = json.loads(await get_entry.handler(resource_id="TESTRES", headword="H4941"))
    assert data["found"] is False
    assert data["status"] == "not_found"
    assert _article_paths(fake) == []


async def test_language_filter_excludes_other_language(monkeypatch):
    routes = {
        "/api/app/books/TESTRES": _book(hint="L.9.0"),
        "/api/app/books/TESTRES/tableofcontents": _toc(
            ("Title", 10), ("ש", 1000), ("ת", 9000)
        ),
        f"/api/app/guides/word?reference={quote('שְׂעַר')}": _EMPTY_GUIDE,
        "/api/app/books/TESTRES/articles/L.1.0": _env(
            "L.1.0", 10, 5, "<div>front</div>", "L.2.0"
        ),
        "/api/app/books/TESTRES/articles/L.2.0": _env(
            "L.2.0", 1000, 100, _span("שְׂעַר", "arc") + "<div>hair</div>", None
        ),
    }
    _use_fake(monkeypatch, routes)
    data = json.loads(
        await get_entry.handler(resource_id="TESTRES", headword="שְׂעַר", language="he")
    )
    assert data["found"] is False
    assert data["status"] == "not_found"


async def test_missing_args_raise(monkeypatch):
    _use_fake(monkeypatch, {})
    with pytest.raises(ValueError, match="resource_id"):
        await get_entry.handler(resource_id="", headword="x")
    with pytest.raises(ValueError, match="headword or article_id"):
        await get_entry.handler(resource_id="TESTRES")


async def test_seek_reuses_shared_cache(monkeypatch):
    """A sibling chunk's cached articles seed the next seek: the second
    lookup for a nearby offset must not re-walk from the top of the book."""
    envs = {
        "C.1.0": _env("C.1.0", 100, 900, "<div>a</div>", "C.2.0"),
        "C.2.0": _env("C.2.0", 1000, 900, "<div>b</div>", "C.3.0"),
        "C.3.0": _env("C.3.0", 1900, 900, "<div>c</div>", None),
    }

    class CountingClient(FakeClient):
        async def get(self, path):
            self.requested.append(path)
            aid = path.rsplit("/articles/", 1)[-1]
            if aid in envs:
                return envs[aid]
            raise _http_404(path)

    fake = CountingClient({})
    monkeypatch.setattr(get_entry, "logos_client", fake)
    cache: dict[str, dict] = {}
    budget = [100]
    first = await get_entry._seek_numeric("C", "C", 1500, cache, budget)
    assert first is not None and first[0] == "C.2.0"
    before = len(fake.requested)
    second = await get_entry._seek_numeric("C", "C", 2000, cache, budget)
    assert second is not None and second[0] == "C.3.0"
    # No top-of-book re-walk: only the forward step past the cached article.
    assert len(fake.requested) - before <= 2


# ── Alphabetic chains (CHALOT-style) ──────────────────────────────────────────


async def test_alpha_chain_section_walk(monkeypatch):
    routes = {
        "/api/app/books/CH": _book("Fake CHALOT", hint="TITLE"),
        "/api/app/books/CH/tableofcontents": _toc(
            ("Title Page", 100), ("צ", 1000), ("ק", 2000)
        ),
        f"/api/app/guides/word?reference={quote('צה')}": _EMPTY_GUIDE,
        "/api/app/books/CH/articles/X": _env("X", 1000, 2, "<div>letter</div>", "X.1"),
        "/api/app/books/CH/articles/X.1": _env(
            "X.1", 1005, 60, _span("צה") + "<div>the entry</div>", "X.2"
        ),
        "/api/app/books/CH/articles/X.2": _env(
            "X.2", 1100, 40, _span("אחר") + "<div>next</div>", None
        ),
    }
    fake = _use_fake(monkeypatch, routes)
    # Every other probed letter id 404s; only X exists.
    data = json.loads(await get_entry.handler(resource_id="CH", headword="צה"))
    assert data["article_id"] == "X.1"
    assert data["indexed_offset"] == 1005
    assert "the entry" in data["text"]
    assert data["status"] == "entry"
    assert data["match"] == "exact"
    assert data["scan_complete"] is True
    assert fake.requested  # the probe map was consulted


# ── Read-only hard rule ───────────────────────────────────────────────────────


def test_tool_module_imports_no_writers_or_engine():
    src = Path(get_entry.__file__).read_text()
    tree = ast.parse(src)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported.extend(a.name for a in node.names)
    forbidden = ("logos.db", "logos.ingest", "logos.tools.ingest_book")
    # research_engine.plugins.sdk is the sanctioned @tool decorator path used
    # by every tool (cf. word_study.py); anything deeper into the engine
    # (storage, corpus, config) is forbidden here.
    for name in imported:
        assert not name.startswith(forbidden), f"writer import: {name}"
        if name.startswith("research_engine"):
            assert name.startswith("research_engine.plugins.sdk"), (
                f"engine import: {name}"
            )
    for symbol in (
        "insert_chunks",
        "save_article_text",
        "upsert_ingest",
        "ingest_drafts",
        "get_article_texts",
    ):
        assert symbol not in src, f"writer symbol referenced: {symbol}"


async def test_dry_run_proves_zero_writes(monkeypatch):
    """Every HTTP path touched is a read endpoint; no DB/corpus call exists
    to make (see import test above), and the trace below proves it."""
    routes = {
        "/api/app/books/DRY": _book(hint="DRY.9.0"),
        "/api/app/books/DRY/tableofcontents": _toc(
            ("Title", 10), ("ב", 1000), ("ג", 2000)
        ),
        f"/api/app/guides/word?reference={quote('בב')}": _EMPTY_GUIDE,
        "/api/app/books/DRY/articles/DRY.1.0": _env(
            "DRY.1.0", 10, 5, "<div>front</div>", "DRY.2.0"
        ),
        "/api/app/books/DRY/articles/DRY.2.0": _env(
            "DRY.2.0", 1000, 50, _span("בב") + "<div>entry</div>", None
        ),
    }
    fake = _use_fake(monkeypatch, routes)
    data = json.loads(await get_entry.handler(resource_id="DRY", headword="בב"))
    assert data["article_id"] == "DRY.2.0"
    assert fake.requested, "expected at least one read"
    for path in fake.requested:
        assert path.startswith("/api/app/"), f"non-API path: {path}"
        assert (
            "/articles/" in path
            or "/guides/word?" in path
            or path.endswith("/tableofcontents")
            or path == "/api/app/books/DRY"
        ), path


# ── Registration ──────────────────────────────────────────────────────────────


def test_tool_id_and_pack_registration():
    assert get_entry.handler._tool_id == "logos.get_entry"
    schema = get_entry.handler._tool_input_schema
    assert "resource_id" in schema["required"]
    pack_path = Path(get_entry.__file__).resolve().parents[2] / "pack.yaml"
    pack = yaml.safe_load(pack_path.read_text())
    tools = {t["id"]: t for t in pack["provides"]["mcp_tools"]}
    assert tools["logos.get_entry"]["entry"] == "logos.tools.get_entry:handler"
    assert "logos.word_study" in tools  # registered alongside word study


# ── v2: gap-tolerant seeking ──────────────────────────────────────────────────


async def test_mid_chain_gap_far_probe_recovery(monkeypatch):
    """An 8-wide mid-chain gap must not cap the seek: far probes prove the
    chain continues and bisection finds the target past the gap."""
    existing = [n for n in range(1, 201) if not 64 <= n <= 71]
    after = {
        n: (existing[i + 1] if i + 1 < len(existing) else None)
        for i, n in enumerate(existing)
    }
    routes = {}
    for n in existing:
        nxt = f"G.{after[n]}.0" if after[n] else None
        routes[f"/api/app/books/G/articles/G.{n}.0"] = _env(
            f"G.{n}.0", n * 100, 100, f"<div>entry {n}</div>", nxt
        )
    fake = _use_fake(monkeypatch, routes)
    found = await get_entry._seek_numeric("G", "G", 15010, {}, [5000])
    assert found is not None and found[0] == "G.150.0"
    # Far probes re-establish bisection past the gap: 31 fetches, not the
    # 98-fetch linear walk v1 falls back to once hi caps at the gap.
    fetches = [p for p in fake.requested if "/articles/" in p]
    assert len(fetches) < 60


# ── v2: chunked scans ─────────────────────────────────────────────────────────


async def test_two_chunk_boundary_no_miss_no_dupe(monkeypatch):
    """Matches on both sides of a chunk boundary are each found exactly once."""
    monkeypatch.setattr(get_entry, "_CHUNK_TARGET_BYTES", 5000)
    routes = {
        "/api/app/books/CH": _book("Fake", hint="CH.9.0"),
        "/api/app/books/CH/tableofcontents": _toc(
            ("Title Page", 10), ("ב", 1000), ("ג", 9000)
        ),
        f"/api/app/guides/word?reference={quote('בב')}": _EMPTY_GUIDE,
        "/api/app/books/CH/articles/CH.1.0": _env(
            "CH.1.0", 10, 5, "<div>front</div>", "CH.2.0"
        ),
        "/api/app/books/CH/articles/CH.2.0": _env(
            "CH.2.0", 1000, 500, _span("בב") + "<div>first</div>", "CH.2.1"
        ),
        "/api/app/books/CH/articles/CH.2.1": _env(
            "CH.2.1", 5000, 500, _span("בב") + "<div>second</div>", "CH.2.2"
        ),
        "/api/app/books/CH/articles/CH.2.2": _env(
            "CH.2.2", 8500, 100, _span("אחר") + "<div>other</div>", None
        ),
    }
    _use_fake(monkeypatch, routes)
    data = json.loads(await get_entry.handler(resource_id="CH", headword="בב"))
    assert data["status"] == "ambiguous"
    assert [c["article_id"] for c in data["candidates"]] == ["CH.2.0", "CH.2.1"]


# ── v2: exhaustion ────────────────────────────────────────────────────────────


async def test_zero_budget_returns_partial_link_candidate(monkeypatch):
    """A spent budget surfaces scan_complete=false with path-1 hits kept."""
    monkeypatch.setattr(get_entry, "MAX_WALK_ARTICLES", 3)
    routes = {
        "/api/app/books/Z": _book("Fake", hint="Z.9.0"),
        "/api/app/books/Z/tableofcontents": _toc(
            ("Title Page", 10), ("ב", 1000), ("ג", 9000)
        ),
        f"/api/app/guides/word?reference={quote('בב')}": _word_guide(
            "lemma.lbs.he.בב", [("Z", "T", "gloss", 1100, 50)]
        ),
        "/api/app/books/Z/articles/Z.1.0": _env(
            "Z.1.0", 10, 5, "<div>front</div>", "Z.2.0"
        ),
        "/api/app/books/Z/articles/Z.2.0": _env(
            "Z.2.0", 1000, 200, _span("בב") + "<div>one</div>", "Z.2.1"
        ),
        "/api/app/books/Z/articles/Z.2.1": _env(
            "Z.2.1", 1200, 200, _span("בב") + "<div>two</div>", "Z.2.2"
        ),
        "/api/app/books/Z/articles/Z.2.2": _env(
            "Z.2.2", 1400, 200, _span("בב") + "<div>three</div>", None
        ),
    }
    _use_fake(monkeypatch, routes)
    data = json.loads(await get_entry.handler(resource_id="Z", headword="בב"))
    assert data["scan_complete"] is False
    # The word-study-endorsed article survived the exhaustion.
    ids = [c["article_id"] for c in data.get("candidates", [])]
    assert "Z.2.0" in ids or data.get("article_id") == "Z.2.0"


# ── v2: deadline ──────────────────────────────────────────────────────────────


async def test_deadline_returns_partial_results(monkeypatch):
    """A hanging scan resolves to the link-seek hit with scan_complete=false,
    within timeout_s + slack — never a dead call."""

    never = asyncio.Event()

    class HangingClient(FakeClient):
        async def get(self, path):
            if path.endswith("/articles/H.2.1"):
                await never.wait()
            return await super().get(path)

    routes = {
        "/api/app/books/H": _book("Fake", hint="H.9.0"),
        "/api/app/books/H/tableofcontents": _toc(
            ("Title Page", 10), ("ב", 1000), ("ג", 9000)
        ),
        f"/api/app/guides/word?reference={quote('בב')}": _word_guide(
            "lemma.lbs.he.בב", [("H", "T", "gloss", 1100, 50)]
        ),
        "/api/app/books/H/articles/H.1.0": _env(
            "H.1.0", 10, 5, "<div>front</div>", "H.2.0"
        ),
        "/api/app/books/H/articles/H.2.0": _env(
            "H.2.0", 1000, 200, _span("בב") + "<div>one</div>", "H.2.1"
        ),
        "/api/app/books/H/articles/H.2.1": _env(
            "H.2.1", 1200, 200, _span("בב") + "<div>two</div>", "H.2.2"
        ),
        "/api/app/books/H/articles/H.2.2": _env(
            "H.2.2", 1400, 200, _span("אחר") + "<div>three</div>", None
        ),
    }
    monkeypatch.setattr(get_entry, "logos_client", HangingClient(routes))

    t0 = time.monotonic()
    data = json.loads(
        await get_entry.handler(resource_id="H", headword="בב", timeout_s=0.5)
    )
    elapsed = time.monotonic() - t0

    assert elapsed < 0.5 + 5
    assert data["scan_complete"] is False
    assert data["status"] == "entry"  # lone link hit, provisional
    assert data["article_id"] == "H.2.0"
    assert data["match"] == "exact"


# ── v2: contract ──────────────────────────────────────────────────────────────


async def test_contract_status_fields(monkeypatch):
    """status on all three shapes; match + scan_complete on entries;
    candidate headwords never blank; language is the matched span's."""
    _use_fake(
        monkeypatch,
        {
            "/api/app/books/C": _book("Fake", hint="C.9.0"),
            "/api/app/books/C/tableofcontents": _toc(("Title Page", 10)),
            "/api/app/books/C/articles/C.1.0": _env(
                "C.1.0", 10, 50, _span("kk", "he") + "<div>entry</div>", None
            ),
        },
    )
    entry = json.loads(await get_entry.handler(resource_id="C", article_id="C.1.0"))
    assert entry["status"] == "entry"
    assert entry["match"] == "exact"
    assert entry["scan_complete"] is True

    _use_fake(
        monkeypatch,
        {
            "/api/app/books/K": _book("Fake", hint="K.9.0"),
            "/api/app/books/K/tableofcontents": _toc(
                ("Title Page", 10), ("k", 1000), ("m", 9000)
            ),
            f"/api/app/guides/word?reference={quote('kk')}": _EMPTY_GUIDE,
            "/api/app/books/K/articles/K.1.0": _env(
                "K.1.0", 10, 5, "<div>front</div>", "K.2.0"
            ),
            "/api/app/books/K/articles/K.2.0": _env(
                "K.2.0", 1000, 100, _span("kk", "he") + "<div>one</div>", "K.2.1"
            ),
            "/api/app/books/K/articles/K.2.1": _env(
                "K.2.1",
                1200,
                100,
                _span("other", "arc") + _span("kk", "he") + "<div>two</div>",
                None,
            ),
        },
    )
    amb = json.loads(await get_entry.handler(resource_id="K", headword="kk"))
    assert amb["status"] == "ambiguous"
    assert amb.get("ambiguous") is True  # compat key until 0.2.0
    assert [c["article_id"] for c in amb["candidates"]] == ["K.2.0", "K.2.1"]
    assert all(c["headword"] for c in amb["candidates"])
    # Matched span's language — not the article's first span (arc on K.2.1).
    assert [c["language"] for c in amb["candidates"]] == ["he", "he"]

    _use_fake(
        monkeypatch,
        {
            "/api/app/books/S": _book("Fake", hint="S.9.0"),
            "/api/app/books/S/tableofcontents": _toc(("Title Page", 10)),
            "/api/app/guides/word?reference=H4941": _EMPTY_GUIDE,
        },
    )
    missing = json.loads(await get_entry.handler(resource_id="S", headword="H4941"))
    assert missing["status"] == "not_found"
    assert missing["found"] is False  # compat key until 0.2.0
    assert missing["candidates"] == []


async def test_zero_resource_length_scans_to_chain_end(monkeypatch):
    """A book root without resourceLength must not collapse the wanted
    last section to a 1-char range — the walk runs to chain end."""
    sections = get_entry._toc_sections(_toc(("Title Page", 10), ("ב", 1000)))
    ranges = get_entry._plan_scan_ranges(sections, "בב", None, [], 0)
    assert ranges == [(1000, get_entry._SCAN_TO_END)]

    routes = {
        "/api/app/books/T": _book("Fake", hint="T.9.0", length=0),
        "/api/app/books/T/tableofcontents": _toc(("Title Page", 10), ("ב", 1000)),
        f"/api/app/guides/word?reference={quote('בב')}": _EMPTY_GUIDE,
        "/api/app/books/T/articles/T.1.0": _env(
            "T.1.0", 10, 5, "<div>front</div>", "T.2.0"
        ),
        "/api/app/books/T/articles/T.2.0": _env(
            "T.2.0", 1000, 200, _span("בב") + "<div>entry</div>", None
        ),
    }
    _use_fake(monkeypatch, routes)
    data = json.loads(await get_entry.handler(resource_id="T", headword="בב"))
    assert data["status"] == "entry"
    assert data["article_id"] == "T.2.0"


async def test_language_filter_drops_link_path_mismatch(monkeypatch):
    """The language filter applies on the link path too: an endorsed
    article with no span in the requested language is dropped, and
    unknown codes resolve to not_found — never match-all."""
    routes = {
        "/api/app/books/Q": _book("Fake", hint="Q.9.0"),
        "/api/app/books/Q/tableofcontents": _toc(("Title Page", 10)),
        f"/api/app/guides/word?reference={quote('qx')}": _word_guide(
            "lemma.lbs.he.qx", [("Q", "T", "gloss", 1100, 50)]
        ),
        "/api/app/books/Q/articles/Q.1.0": _env(
            "Q.1.0", 10, 5, "<div>front</div>", "Q.2.0"
        ),
        "/api/app/books/Q/articles/Q.2.0": _env(
            "Q.2.0",
            1100,
            50,
            _span("שְׂעַר", "arc") + "<div>hair</div>",
            None,
        ),
    }
    _use_fake(monkeypatch, routes)

    hebrew = json.loads(
        await get_entry.handler(resource_id="Q", headword="qx", language="he")
    )
    assert hebrew["status"] == "not_found"

    unknown = json.loads(
        await get_entry.handler(resource_id="Q", headword="qx", language="xx")
    )
    assert unknown["status"] == "not_found"

    aramaic = json.loads(
        await get_entry.handler(resource_id="Q", headword="qx", language="arc")
    )
    assert aramaic["status"] == "entry"
    assert aramaic["match"] == "offset"  # endorsed, span text never matched
    assert aramaic["headword"] == "qx"  # guide-lemma fallback, never blank


async def test_offset_tier_falls_back_to_queried_headword(monkeypatch):
    """Without a guide lemma the offset label is the queried headword."""
    routes = {
        "/api/app/books/Q": _book("Fake", hint="Q.9.0"),
        "/api/app/books/Q/tableofcontents": _toc(("Title Page", 10)),
        f"/api/app/guides/word?reference={quote('qx')}": _word_guide(
            None, [("Q", "T", "gloss", 1100, 50)]
        ),
        "/api/app/books/Q/articles/Q.1.0": _env(
            "Q.1.0", 10, 5, "<div>front</div>", "Q.2.0"
        ),
        "/api/app/books/Q/articles/Q.2.0": _env(
            "Q.2.0",
            1100,
            50,
            _span("שְׂעַר", "arc") + "<div>hair</div>",
            None,
        ),
    }
    _use_fake(monkeypatch, routes)
    data = json.loads(await get_entry.handler(resource_id="Q", headword="qx"))
    assert data["status"] == "entry"
    assert data["match"] == "offset"
    assert data["headword"] == "qx"
