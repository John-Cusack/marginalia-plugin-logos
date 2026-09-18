"""Research Engine 0.6 discovers, enables, migrates, loads and runs this plugin.

The proofs the release gate asks for, against an installed core and this plugin
installed as a distribution — never a checkout on `PYTHONPATH`:

1. discovery reads the manifest without importing `logos`;
2. audit lists every tool, chunker, filter, schema, permission and migration;
3. enable records the exact version and manifest hash, and a changed version
   needs approving again;
4. migrate brings the plugin's tables to the declared revision, and core refuses
   to load it while that is pending;
5. load registers every contribution, or none of them;
6. `logos.auth_status` and `logos.ingest_status` answer through core's dispatch;
7. a walk killed by an embedding outage resumes and stores the book, with the
   passages, node tree and embeddings core wrote checked in the database.

Skipped unless core 0.6 is installed and `LOGOS_TEST_DB_URL` names a server
where databases may be created; each run makes its own and drops it. The
`core-integration` CI job provides both.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import importlib.util
import json
import os
import subprocess
import sys
import threading
import uuid
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

pytestmark = [pytest.mark.integration]

if importlib.util.find_spec("research_engine") is None:
    pytest.skip("needs research-engine installed", allow_module_level=True)
if importlib.util.find_spec("research_engine.plugins.discovery") is None:
    pytest.skip("needs research-engine 0.6 (entry-point discovery)", allow_module_level=True)
ADMIN_URL = os.environ.get("LOGOS_TEST_DB_URL")
if not ADMIN_URL:
    pytest.skip("needs LOGOS_TEST_DB_URL (a disposable server)", allow_module_level=True)

import sqlalchemy as sa
from research_engine.composition import build_container
from research_engine.config import load_settings
from research_engine.domain.filter_extension import FilterExtension
from research_engine.domain.nodes import build_node_tree as core_build_node_tree
from research_engine.domain.provenance import PluginActivationState
from research_engine.mcp.dispatch import dispatch_tool
from research_engine.plugins.activation import PluginActivationManager
from research_engine.plugins.discovery import scan_plugins
from research_engine.plugins.loader import PluginLoader
from research_engine.plugins.registry import PluginRegistry
from research_engine.testing import ensure_test_database
from research_engine_sdk import parse_manifest

from logos.db.migrate import read_status
from logos.db.pool import close_pool
from logos.filters import ScriptureRefRangeFilter
from logos.ingest.nodes import build_node_tree
from logos.tools import ingest_book
from tests.fake_logos import ORDER, RESOURCE, TITLE, FakeLogos

if TYPE_CHECKING:
    from collections.abc import Iterator

DIST = importlib.metadata.distribution("marginalia-ai-plugin-logos")
MANIFEST = parse_manifest(
    Path(DIST.locate_file({str(f): f for f in DIST.files or []}["logos/plugin.yaml"]))
)
#: Core's own default; the fake server must answer as this model or the client
#: refuses to store a vector.
EMBEDDING_MODEL = "BAAI/bge-m3"
EMBEDDING_DIM = 1024


class FakeEmbedServer:
    """`research-engine embed-server` in ~40 lines: deterministic vectors.

    Real vectors need a 2.3 GB model and a GPU. These are nonsense, but they are
    the right model name and dimension, which is all core checks before storing.

    `serving = False` drops the connection rather than answering 503, because
    that is the outage core reports as `EmbeddingUnavailable`: its remote client
    translates `httpx.TransportError` only, so an embed server that *answers*
    with a 5xx reaches the caller as a raw `HTTPStatusError` until the circuit
    breaker opens. A GPU host going away mid-run is the case this plugin's
    two-phase store was built for, and it is a transport error.
    """

    def __init__(self) -> None:
        self.serving = True
        self.embedded = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # noqa: A002 - silence the server
                pass

            def _send(self, code: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
                self._send(200, {
                    "status": "ok", "model_name": EMBEDDING_MODEL,
                    "model_version": "1.0", "dim": EMBEDDING_DIM,
                    "device": "fake", "warm": True, "concurrency": 1,
                })

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
                if not outer.serving:
                    self.close_connection = True
                    self.connection.close()
                    return
                length = int(self.headers.get("Content-Length", 0))
                texts = json.loads(self.rfile.read(length))["texts"]
                outer.embedded += len(texts)
                self._send(200, {
                    "embeddings": [
                        [((hash(text) % 1000) / 1000.0 + index) % 1.0] * EMBEDDING_DIM
                        for index, text in enumerate(texts)
                    ],
                    "model_name": EMBEDDING_MODEL,
                    "model_version": "1.0",
                    "dim": EMBEDDING_DIM,
                })

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> FakeEmbedServer:
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture(scope="module")
def embed_server() -> Iterator[FakeEmbedServer]:
    with FakeEmbedServer() as server:
        yield server


def _corpus_url(name: str) -> str:
    return sa.engine.make_url(
        ADMIN_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
    ).set(database=name).render_as_string(hide_password=False)


async def _drop_database(name: str) -> None:
    admin = sa.ext.asyncio.create_async_engine(
        _corpus_url("postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        async with admin.connect() as conn:
            await conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        await admin.dispose()


@pytest.fixture(scope="module")
def corpus_url() -> Iterator[str]:
    """A database of its own, built by core's migrations, dropped afterwards.

    Synchronous on purpose: every async fixture here is per-test, because a
    connection pool belongs to the event loop that opened it and pytest-asyncio
    gives each test its own.
    """
    name = f"logos_core_it_{uuid.uuid4().hex[:12]}"
    url = _corpus_url(name)
    if not asyncio.run(ensure_test_database(url)):
        pytest.skip(f"{ADMIN_URL} is unreachable or cannot create databases")
    try:
        yield url
    finally:
        asyncio.run(_drop_database(name))


@pytest.fixture(scope="module")
def engine_env(
    corpus_url: str, embed_server: FakeEmbedServer, tmp_path_factory
) -> Iterator[dict[str, str]]:
    """The environment an operator's engine runs with, pointed at throwaway state."""
    data_dir = tmp_path_factory.mktemp("re-data")
    env = {
        "RE_DB_URL": corpus_url,
        "RE_DATA_DIR": str(data_dir),
        "RE_EMBEDDING_PROVIDER": "remote_api",
        "RE_INFERENCE_BASE_URL": embed_server.base_url,
        "RE_RERANKER_PROVIDER": "none",
    }
    previous = {key: os.environ.get(key) for key in (*env, "PYTHONPATH", "RE_ENV_FILE")}
    os.environ.update(env)
    os.environ.pop("PYTHONPATH", None)
    os.environ.pop("RE_ENV_FILE", None)
    try:
        yield {**os.environ}
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture(scope="module")
def discovered(engine_env):
    (plugin,) = [p for p in scan_plugins().plugins if p.plugin_id == "logos"]
    return plugin


@pytest.fixture
async def container(engine_env):
    """A real container: repositories, clients and the plugin loader core uses."""
    built = await build_container(load_settings())
    try:
        yield built
    finally:
        await built.close()
        # The plugin's own pool is process-global and belongs to this test's loop.
        await close_pool()


@pytest.fixture
async def enabled(container, discovered, engine_env):
    """The plugin approved and migrated, as an operator's two commands leave it."""
    manager = PluginActivationManager(container.plugin_activations)
    await manager.approve("logos", non_interactive=True, discovered_plugins=[discovered])
    await manager.migrate(
        "logos",
        database_url=load_settings().db_url,
        data_root=Path(engine_env["RE_DATA_DIR"]) / "plugin-data",
        discovered_plugins=[discovered],
    )
    return manager


async def _loaded_registry(container, discovered) -> tuple[PluginRegistry, list[str]]:
    registry = PluginRegistry()
    registry.register_core_types()
    loader = PluginLoader(
        container.plugin_activations,
        registry,
        Path(os.environ["RE_DATA_DIR"]) / "plugin-data",
    )
    return registry, await loader.load_enabled([discovered])


def test_the_plugin_is_installed_as_a_distribution_not_a_checkout() -> None:
    origin = Path(importlib.util.find_spec("logos").origin).resolve()
    assert "site-packages" in origin.parts, origin


def test_discovery_reads_the_manifest_without_importing_the_plugin() -> None:
    script = (
        "import sys\n"
        "from research_engine.plugins.discovery import scan_plugins\n"
        "report = scan_plugins()\n"
        "assert not report.issues, report.issues\n"
        "ids = [p.plugin_id for p in report.plugins]\n"
        "assert 'logos' in ids, ids\n"
        "assert 'logos' not in sys.modules, 'discovery imported the plugin'\n"
    )
    subprocess.run([sys.executable, "-c", script], check=True, timeout=120)


def test_discovery_records_the_distribution_and_its_manifest(discovered) -> None:
    assert discovered.distribution_name == "marginalia-ai-plugin-logos"
    assert discovered.distribution_version == DIST.version
    assert discovered.manifest.model_dump() == MANIFEST.model_dump()
    assert discovered.manifest_sha256 and len(discovered.manifest_sha256) == 64
    for resource in MANIFEST.resource_paths():
        assert discovered.resource_path(resource).is_file()


def test_audit_lists_every_contribution_and_permission(engine_env) -> None:
    """Core's own `plugin audit`, as an operator reads it before approving."""
    audit = subprocess.run(
        [str(Path(sys.executable).with_name("research-engine")), "plugin", "audit", "logos"],
        capture_output=True, text=True, timeout=300, env=engine_env, check=True,
    ).stdout
    # Rich wraps the JSON it prints, so compare on text with whitespace removed.
    flat = "".join(audit.split())
    for tool in MANIFEST.provides.mcp_tools:
        assert tool.id in flat, f"audit omits {tool.id}"
    for contribution in (
        *MANIFEST.provides.chunkers,
        *MANIFEST.provides.filter_extensions,
        *MANIFEST.provides.extraction_schemas,
        *MANIFEST.provides.document_types,
    ):
        assert contribution.id in flat
    for host in MANIFEST.permissions.network_allowlist:
        assert host in flat
    assert '"subprocess":true' in flat and '"ingest":true' in flat
    assert '"llm":false' in flat and '"write":false' in flat
    assert "logos.db.migrate:upgrade" in flat and "revision1" in flat


async def test_enable_records_the_exact_artifact(container, discovered, enabled) -> None:
    activation = await container.plugin_activations.get("logos")
    assert activation.distribution_version == discovered.distribution_version
    assert activation.manifest_sha256 == discovered.manifest_sha256
    assert activation.entry_point_name == "logos"
    assert activation.enabled and activation.state is PluginActivationState.enabled
    assert activation.permissions_granted["network_allowlist"] == [
        "app.logos.com", "www.logos.com", "auth.faithlife.com"
    ]


async def test_migrate_brought_the_plugin_tables_to_the_declared_revision(
    container, corpus_url, enabled
) -> None:
    activation = await container.plugin_activations.get("logos")
    assert activation.database_revision == MANIFEST.provides.database.current_revision
    assert activation.database_status == "ok"

    dsn = corpus_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    state = await read_status(dsn)
    assert state.up_to_date and state.current_revision == 1

    engine = sa.ext.asyncio.create_async_engine(corpus_url)
    try:
        async with engine.connect() as conn:
            tables = set(
                (await conn.execute(sa.text(
                    "SELECT tablename FROM pg_tables WHERE tablename LIKE 'logos\\_%'"
                ))).scalars()
            )
    finally:
        await engine.dispose()
    assert "logos_ingest_progress" in tables and "logos_schema_migrations" in tables


async def test_a_pending_migration_keeps_the_plugin_from_loading(
    container, discovered, enabled
) -> None:
    """Core's rule: a declared revision ahead of the approved one blocks load."""
    ahead = replace(
        discovered,
        manifest=discovered.manifest.model_copy(
            update={"provides": discovered.manifest.provides.model_copy(
                update={"database": discovered.manifest.provides.database.model_copy(
                    update={"current_revision": 99}
                )}
            )}
        ),
    )
    manager = PluginActivationManager(container.plugin_activations)
    (status,) = await manager.inventory(discovered_plugins=[ahead])
    assert status.state is PluginActivationState.error
    assert "migration required" in status.reason

    registry, loaded = await _loaded_registry(container, ahead)
    assert loaded == [] and registry.get_mcp_tools() == {}


async def test_a_different_version_needs_approving_again(
    container, discovered, enabled
) -> None:
    upgraded = replace(discovered, distribution_version="99.0.0")
    manager = PluginActivationManager(container.plugin_activations)

    (status,) = await manager.inventory(discovered_plugins=[upgraded])

    assert status.state is PluginActivationState.pending_approval
    registry, loaded = await _loaded_registry(container, upgraded)
    assert loaded == [] and registry.get_mcp_tools() == {}


async def test_load_registers_every_contribution(container, discovered, enabled) -> None:
    registry, loaded = await _loaded_registry(container, discovered)

    assert loaded == ["logos"]
    assert set(registry.get_mcp_tools()) == {t.id for t in MANIFEST.provides.mcp_tools}
    assert registry.resolve_chunker("verse_boundary") is not None
    assert "logos_book" in registry.list_document_types()
    assert "scripture_ref_range" in registry.get_filter_extensions()
    assert "scripture_cross_refs" in {row[0] for row in registry.get_extraction_schemas()}
    specs = registry.get_mcp_tool_specs()
    for tool in MANIFEST.provides.mcp_tools:
        assert specs[tool.id].input_schema == tool.input_schema


async def test_one_broken_entry_registers_nothing(container, discovered, enabled) -> None:
    """All or nothing: a plugin that half-loads is worse than one that does not."""
    tools = list(discovered.manifest.provides.mcp_tools)
    tools[3] = tools[3].model_copy(update={"entry": "logos.tools.absent:handler"})
    broken = replace(
        discovered,
        manifest=discovered.manifest.model_copy(
            update={"provides": discovered.manifest.provides.model_copy(
                update={"mcp_tools": tools}
            )}
        ),
    )
    manager = PluginActivationManager(container.plugin_activations)
    await manager.approve("logos", non_interactive=True, discovered_plugins=[broken])
    try:
        registry, loaded = await _loaded_registry(container, broken)
        assert loaded == []
        assert registry.get_mcp_tools() == {}
        assert registry.get_filter_extensions() == {}
        assert "logos_book" not in registry.list_document_types()
    finally:
        await manager.approve(
            "logos", non_interactive=True, discovered_plugins=[discovered]
        )
        await container.plugin_activations.record_migration(
            "logos", revision=1, status="ok",
            state=PluginActivationState.enabled, last_error=None,
        )


def test_the_filter_extension_satisfies_cores_protocol() -> None:
    assert isinstance(ScriptureRefRangeFilter(), FilterExtension)


@pytest.mark.parametrize(
    "sections",
    [
        [],
        [{"char_start": 0, "char_end": 10, "level": 1, "heading": "One"}],
        [
            {"char_start": 0, "char_end": 5, "level": 1, "heading": "Letter"},
            {"char_start": 5, "char_end": 40, "level": 2, "heading": "Entry",
             "article_id": "A.1"},
            {"char_start": 40, "char_end": 90, "level": 6, "heading": "Deep",
             "article_id": "A.1.1.1.1"},
            {"char_start": 90, "char_end": 99, "level": 2, "heading": "Next",
             "article_id": "A.2"},
        ],
        [{"char_start": 0, "char_end": 7}, {"char_start": 7, "char_end": 9}],
    ],
)
def test_the_plugins_node_tree_is_the_tree_core_would_have_built(sections) -> None:
    """0.1.x called core's `build_node_tree`; 0.2.0 ships the same rule.

    A book stored by 0.2.0 must have the outline 0.1.x gave it, node for node.
    """
    mine = build_node_tree(sections, text_length=100, title="Book")
    theirs = core_build_node_tree(sections, text_length=100, title="Book")

    assert [n.model_dump() for n in mine] == [n.model_dump() for n in theirs]


class TestServedThroughCore:
    """Core dispatches to the loaded handlers, with the scoped clients it built."""

    @pytest.fixture(autouse=True)
    async def loaded(self, container, discovered, enabled):
        await container.plugin_loader.load_enabled([discovered])
        assert container.plugin_registry.get_mcp_tools(), "nothing loaded"

    async def test_auth_status_reports_no_session_in_the_plugin_data_dir(
        self, container, engine_env
    ) -> None:
        result = json.loads(await dispatch_tool(container, "logos.auth_status"))

        assert result["authenticated"] is False
        expected = Path(engine_env["RE_DATA_DIR"]) / "plugin-data" / "logos"
        assert str(expected) in json.dumps(result), result

    async def test_ingest_status_reads_the_plugins_own_tables(self, container) -> None:
        result = await dispatch_tool(container, "logos.ingest_status")

        assert result["books"] == [] or isinstance(result["books"], list)


class TestResumableIngestThroughCore:
    """The release gate's fixture ingest: a real book, real Postgres, real core.

    The embedding host is down for the first store, as it was for the corpus run
    this two-phase design came from. Nothing may be lost, duplicated or stored
    at the wrong offset across the outage.
    """

    @pytest.fixture(autouse=True)
    def fake_api(self, monkeypatch):
        api = FakeLogos()
        monkeypatch.setattr(ingest_book, "logos_client", api)
        return api

    @pytest.fixture
    async def clients(self, container, discovered, enabled):
        await container.plugin_loader.load_enabled([discovered])
        return container.plugin_loader.build_plugin_clients("logos")

    async def test_the_book_survives_an_embedding_outage_and_lands_whole(
        self, container, clients, corpus_url, embed_server, fake_api
    ) -> None:
        embed_server.serving = False
        first = await ingest_book.handler(resource_id=RESOURCE, **clients)
        assert first["walk_status"] == "complete"
        assert first["store_status"] == "embedding_unavailable"

        walked = len(fake_api.fetched)
        embed_server.serving = True
        second = await ingest_book.handler(resource_id=RESOURCE, **clients)

        assert second["store_status"] == "complete"
        assert len(fake_api.fetched) == walked, "the store resumed without re-walking"
        document_id = second["document_id"]
        assert document_id

        engine = sa.ext.asyncio.create_async_engine(corpus_url)
        try:
            async with engine.connect() as conn:
                document = (await conn.execute(sa.text(
                    "SELECT title, document_type, source FROM core.documents WHERE id = :i"
                ), {"i": document_id})).one()
                passages = (await conn.execute(sa.text(
                    "SELECT char_start, char_end, text, node_id, chunker, chunker_version "
                    "FROM core.passages WHERE document_id = :i ORDER BY position"
                ), {"i": document_id})).all()
                embedded = (await conn.execute(sa.text(
                    "SELECT count(*) FROM core.passage_embeddings e "
                    "JOIN core.passages p ON p.id = e.passage_id WHERE p.document_id = :i"
                ), {"i": document_id})).scalar_one()
                nodes = (await conn.execute(sa.text(
                    "SELECT path::text, depth, title, char_start, char_end, metadata "
                    "FROM core.document_nodes WHERE document_id = :i ORDER BY path"
                ), {"i": document_id})).all()
                staged = (await conn.execute(sa.text(
                    "SELECT status, count(*) FROM logos_ingest_chunks "
                    "WHERE resource_id = :r GROUP BY status"
                ), {"r": RESOURCE})).all()
                progress = (await conn.execute(sa.text(
                    "SELECT walk_complete, last_article_id FROM logos_ingest_progress "
                    "WHERE resource_id = :r"
                ), {"r": RESOURCE})).one()
                full_text = (await conn.execute(sa.text(
                    "SELECT text FROM core.document_texts WHERE document_id = :i"
                ), {"i": document_id})).scalar_one()
        finally:
            await engine.dispose()

        assert (document.title, document.document_type) == (TITLE, "logos_book")
        assert document.source == f"logos:{RESOURCE}"
        assert passages and len(passages) == second["total_stored"]
        assert embedded == len(passages), "every passage is embedded"

        # Every passage still quotes the canonical text at its own offsets.
        for passage in passages:
            assert passage.text == full_text[passage.char_start : passage.char_end]
            assert passage.chunker == "verse_boundary" and passage.chunker_version == "5.0"
            assert passage.node_id is not None, "passages are attached to their section"

        # The tree: a root over the whole book, and every article as a node
        # whose span holds its own text.
        by_path = {node.path: node for node in nodes}
        root = by_path["r"]
        assert (root.depth, root.title, root.char_start) == (0, TITLE, 0)
        assert root.char_end == len(full_text)
        articles = {
            node.metadata["article_id"]: node
            for node in nodes
            if node.metadata and node.metadata.get("article_id")
        }
        assert set(articles) == set(ORDER)
        child, parent = articles["A.2.1"], articles["A.2"]
        assert child.path.startswith(parent.path + ".") and child.depth == parent.depth + 1

        assert dict(staged) == {"stored": len(passages)}
        # The walk records completion by clearing the cursor, so a resumed run
        # starts from the beginning of an already-walked book and stores only.
        assert progress.walk_complete and progress.last_article_id == ""

    async def test_a_second_run_does_not_store_the_book_twice(
        self, container, clients, corpus_url
    ) -> None:
        again = await ingest_book.handler(resource_id=RESOURCE, **clients)

        assert again["walk_status"] == "already_complete"
        assert again["store_status"] == "nothing_pending"
        engine = sa.ext.asyncio.create_async_engine(corpus_url)
        try:
            async with engine.connect() as conn:
                documents = (await conn.execute(sa.text(
                    "SELECT count(*) FROM core.documents WHERE source = :s"
                ), {"s": f"logos:{RESOURCE}"})).scalar_one()
        finally:
            await engine.dispose()
        assert documents == 1
