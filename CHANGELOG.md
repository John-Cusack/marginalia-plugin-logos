# Changelog

All notable changes to `marginalia-ai-plugin-logos`. Versions follow
[PEP 440](https://peps.python.org/pep-0440/); while the version is 0.x, a minor
release may break things.

## [0.2.0] - Unreleased

The first release as a Python package. Everything below marked **Breaking**
needs action from anyone running 0.1.x; see "Upgrading from 0.1.x" in the
README.

### Breaking: packaging and installation

- The distribution is now `marginalia-ai-plugin-logos` (was
  `marginalia-plugin-logos`, and never published under either name). Install it
  with pip or pipx into Research Engine's environment; installing by cloning
  this repository is no longer supported. The import package is still `logos`
  and the plugin id is still `logos`.
- Research Engine finds the plugin through the `research_engine.plugins` entry
  point `logos = "logos"`. Nothing loads until you run
  `research-engine plugin enable logos`.
- The runtime manifest moved from the root `pack.yaml` to `logos/plugin.yaml`,
  schema v2. Identity, version, license and Python dependencies now live only in
  package metadata; `requires.pip` is gone. Every tool declares its
  `input_schema` there, because that is the schema core validates calls against;
  a test fails if it drifts from the handler's own.
- Extraction schemas moved into the package, at
  `logos/schemas/extraction_schemas/`.
- The license is Apache-2.0, matching the repository's `LICENSE`. Earlier
  metadata said MIT.
- Requires Research Engine 0.6.x (`core_api: ">=0.6,<0.7"`, distributed as
  `marginalia-ai`) and depends on `marginalia-ai-sdk>=0.6,<0.7`.

### Breaking: SDK boundary

- The plugin no longer imports `research_engine`. Decorators, protocols,
  `PassageDraft`, `EmbeddingUnavailable`, the filter protocol and the chunking
  helpers all come from `research_engine_sdk`.
- `logos.ingest_book` builds the document tree itself, as SDK `NodeDraft`s with
  their own paths, instead of calling core's `build_node_tree`. The rule is
  unchanged, so a book stored by 0.2.0 has the outline 0.1.x gave it; the
  integration suite checks the two builders against each other node for node.
- `verse_boundary` is unchanged at **5.0**. A digest test shows the SDK-based
  chunker produces byte-identical passages on every contract fixture, so no
  reindex is needed.
- An embedding outage is recognised whichever class the engine raises. The SDK
  exports `EmbeddingUnavailable`, but Research Engine 0.6 raises its own class of
  that name, so catching only the SDK's meant a dead embedding host looked like
  an ordinary failure — and the store went back to halving batches against it.

### Breaking: plugin data

- The Logos session moved from `~/.logos-mcp/` to the plugin data directory,
  `~/.research-engine/plugin-data/logos/` or `$RE_DATA_DIR/plugin-data/logos/`.
  Inside the engine the directory comes from the plugin context core passes.
- New `logos-login --migrate-data [--from PATH] [--dry-run] [--keep-source]`
  moves `cookies.json` and `browser-profile/` there:
  - it refuses if the destination already holds something different;
  - it keeps the files private (cookies `0600`, directories `0700`);
  - it removes the originals only after verifying the copies;
  - it never prints cookie values.
  Nothing moves until you run it.
- Credentials for pre-filling the login form are no longer read from a
  `MarginaliaAI/.env` found next to the plugin's source.

### Breaking: database migrations

- Tools no longer run `CREATE TABLE IF NOT EXISTS` on first use. The plugin's
  tables are versioned migrations under `logos/db/migrations/`. They are applied
  by `research-engine plugin migrate logos`, or by `python -m logos.db.migrate
  upgrade` outside the engine.
- The manifest declares `logos.db.migrate:status` and `:upgrade`; core calls them
  with its own `database_url` and reads the revision they report. Everywhere
  else the plugin opens its own connection from `RE_DB_URL`, because core 0.6
  passes tool handlers none — see "Database" in the README.
- Migration `001_initial` adopts existing `logos_*` tables as they are and adds
  the `logos_schema_migrations` ledger. Tested on a full copy of a real database:
  row counts and ingest progress were unchanged.
- Each migration runs in its own transaction under an advisory lock and records
  its checksum. A changed checksum blocks further upgrades. No migration drops or
  deletes data.

### Added

- `logos.get_entry`: the full verbatim text of a lexicon or dictionary entry,
  looked up by headword.
- The unauthenticated product-page lookup behind `logos.ingest_book(url=...)`
  goes through the engine's scoped `HttpClient` when one is provided.
- Tests:
  - an end-to-end resumable-ingest test, checked against SDK drafts and the
    canonical text;
  - a core integration suite that drives Research Engine 0.6's own discovery,
    audit, approval, migration and load, then ingests a fixture book through it
    and checks the passages, node tree, embeddings and checkpoints it wrote;
  - package-contract tests covering the manifest, entry point, no-import
    discovery and the absence of `research_engine` imports;
  - artifact checks for wheel and sdist contents and metadata.
- Declared dependencies that had only been satisfied by core being installed:
  `structlog` and `sqlalchemy`.

### Fixed

- `scripture_ref_range` runs against the corpus's `json` metadata column, and
  accepts the full book names the corpus stores.

## [0.1.0]

Initial version, installed from a Git checkout. Never published to PyPI.
