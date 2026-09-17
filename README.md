# marginalia-ai-plugin-logos

A [Research Engine](https://github.com/John-Cusack/MarginaliaAI) plugin for
[Logos Bible Software](https://www.logos.com). It adds:

- **28 MCP tools** over your Logos account: passage text in several
  translations, passage and exegetical guides, word studies, verbatim lexicon
  entries, cross-references, commentary lookup, full-text and library search,
  Factbook, notes, and the Logos study assistant.
- **A verse-boundary chunker** (`verse_boundary` 5.0) built for commentaries and
  Greek/Hebrew reference works, with token budgets measured per script.
- **Resumable whole-book ingestion** (`logos.ingest_book`): walks a book's
  articles, checkpoints every one, and stores the book into your Research Engine
  corpus as a single document with its structure and page numbers.
- **Scholar authority records** and a `scripture_ref_range` search filter.

You need a Logos account. The plugin reads only what that account can open.

> This project is not affiliated with or endorsed by Faithlife, LLC. "Logos" and
> "Faithlife" are their trademarks.

## Compatibility

| Plugin | Research Engine (`marginalia-ai`) and `marginalia-ai-sdk` | Python | Database |
|---|---|---|---|
| 0.2.x | 0.6.x (`core_api: ">=0.6,<0.7"`) | 3.11–3.13 | The PostgreSQL database Research Engine uses |

The wheel depends on `marginalia-ai-sdk`, not on `marginalia-ai` itself;
`logos/plugin.yaml` declares which core versions it runs under, and core refuses
to load it under any other.

## Install

Install into the **same Python environment** as Research Engine.

With pip:

```bash
python -m pip install marginalia-ai marginalia-ai-plugin-logos
```

With pipx, inject it into Research Engine's environment. `--include-apps` puts
`logos-login` and `logos-diagnose` on your `PATH`:

```bash
pipx install marginalia-ai
pipx inject --include-apps marginalia-ai 'marginalia-ai-plugin-logos[auth]'
```

### Browser sign-in (`auth` extra)

Signing in, and renewing a session without a password, uses Playwright and
Chromium. Install the extra, then the browser. Installing a wheel never
downloads a browser for you; this is always a separate step:

```bash
python -m pip install 'marginalia-ai-plugin-logos[auth]'
python -m playwright install chromium
```

Under pipx, run Playwright from Research Engine's environment:

```bash
"$(pipx environment --value PIPX_LOCAL_VENVS)/marginalia-ai/bin/python" -m playwright install chromium
```

Without the extra every tool still works while a stored session is valid;
renewing it then needs `logos-login` from an environment that has Playwright.

## Sign in

```bash
logos-login            # opens a browser once; sign in to Logos
logos-login --status   # where the session is stored, and whether it is valid
logos-login --refresh  # renew silently from the saved browser profile
logos-diagnose         # every layer of auth state, with no cookie values
```

The session lives in the plugin's data directory:

```text
~/.research-engine/plugin-data/logos/     # or $RE_DATA_DIR/plugin-data/logos/
  cookies.json                            # 0600
  browser-profile/                        # 0700, Chromium profile for silent renewal
```

The console scripts work out that location the same way the engine does,
without importing it, so a terminal login is picked up by a running server. If
you set `RE_DATA_DIR` for the engine in a `.env` file rather than the
environment, export it in your shell as well.
[AUTHENTICATION.md](https://github.com/John-Cusack/marginalia-plugin-logos/blob/main/AUTHENTICATION.md)
covers renewal and troubleshooting.

## Enable in Research Engine

Installing the wheel only makes the plugin *discoverable*. Core reads
`logos/plugin.yaml` without importing any plugin code, shows you what it asks
for, and does nothing until you approve it:

```bash
research-engine plugin list              # logos appears as "available"
research-engine plugin audit logos       # tools, chunker, filter, schema, permissions, migrations
research-engine plugin enable logos      # review and approve
research-engine plugin migrate logos     # create or upgrade the plugin's tables
research-engine plugin doctor logos
```

Then restart `research-engine serve`. The tools appear as `logos.*`, for
example `logos.auth_status`, `logos.get_entry`, `logos.library` and
`logos.search`.

After `pip install --upgrade marginalia-ai-plugin-logos`, core holds the new
version until you approve it again, then asks for the migration if the release
added one:

```bash
research-engine plugin approve-upgrade logos
research-engine plugin migrate logos
```

## Upgrading from 0.1.x

0.1.x was installed by cloning this repository. 0.2.0 is a normal package, and
three things change for you:

1. **Your session moves.** 0.1.x kept it in `~/.logos-mcp/`. With the engine
   stopped:

   ```bash
   logos-login --migrate-data --dry-run   # show what would move
   logos-login --migrate-data
   ```

   Only `cookies.json` and `browser-profile/` move. The command refuses and
   changes nothing if the destination already holds something different. It
   verifies the copies before it removes the originals. `--keep-source` keeps
   the originals anyway. Nothing moves until you run it.

2. **Tables are migrated, not created on first use.** 0.1.x created the
   `logos_*` tables the first time a tool needed one. `research-engine plugin
   migrate logos` now records them as revision 1. Existing tables, rows and
   ingest checkpoints are kept exactly as they are, so a half-walked book
   resumes where it stopped.

3. **Nothing needs re-chunking.** `verse_boundary` is still 5.0, and every
   passage boundary is where 0.1.x put it.

The old checkout under `~/.research-engine/plugins/` is no longer loaded. Delete
it once 0.2.0 is enabled and working.

## Database

The plugin keeps its own tables in Research Engine's PostgreSQL database:

| Table | Holds |
|---|---|
| `logos_ingest_progress` | Where each book's walk stopped, for resume |
| `logos_ingest_chunks` | Staged passages, before and after they reach the corpus |
| `logos_ingest_article_texts` | The article text each passage's offsets address |
| `logos_scholars`, `logos_authority` | Scholar authority records |
| `logos_resources`, `logos_api_calls` | Resource tracking and an API call log |
| `logos_schema_migrations` | Which plugin migrations have run, with checksums |

Only this plugin writes these tables. Core's migrations never change them,
uninstalling the plugin never drops them, and no plugin migration deletes data.
Migrations run only when you ask. A migration file that changed after it was
applied blocks every later upgrade until you look at it.

The plugin connects on its own, using `RE_DB_URL` and then `DATABASE_URL` from
the environment, falling back to the engine's own default
(`postgresql://re_dev:re_dev_pass@localhost:5435/research_engine`). Core passes
it no connection, so **`RE_DB_URL` must be in the engine's environment**, not
only in an `.env` file the engine reads for itself: the plugin's tools do not
read that file. `research-engine plugin migrate logos` is the exception — there
core passes the database it is configured with.

To check or migrate from a shell:

```bash
python -m logos.db.migrate status
python -m logos.db.migrate upgrade
```

## Permissions, and what they do not mean

| `plugin.yaml` asks for | Why |
|---|---|
| `network: egress` to `app.logos.com`, `www.logos.com`, `auth.faithlife.com` | The Logos API, the product-page lookup behind `ingest_book(url=...)`, and Faithlife sign-in |
| `subprocess: true` | Silent session renewal launches headless Chromium |
| `filesystem: plugin_data` | `cookies.json` and the browser profile |
| `ingest: true` | `logos.ingest_book` stores books into your corpus |

Database access is not one of them: Research Engine 0.6 has no database
permission to ask for, and hands plugins no connection. The tables above are the
plugin's own, and it opens its own connection to reach them — see "Database".

**An enabled plugin is trusted code.** It runs inside the Research Engine
process as ordinary Python. The permissions limit the SDK clients core hands the
plugin. They are not a sandbox, and they cannot stop code that uses Python
directly. Enable only plugins you trust.

That applies to this plugin in two known places:

- **Direct HTTP.** Logos API calls go through the plugin's own `httpx` client,
  because they need a session cookie jar, retry after a 401, and streaming
  responses from the study assistant, and the SDK's scoped `HttpClient` has none
  of those. Those calls reach only `app.logos.com`. The one unauthenticated
  fetch, the product-page lookup, uses the scoped client.
- **Chromium.** The sign-in page that renewal loads also pulls in hosts outside
  the allowlist (captcha and analytics). Browser traffic is not bound by the
  allowlist.

## Licensed content

Logos resources are licensed to your account. The plugin fetches only what your
account can open and stores it in *your* local corpus for your own study. Do not
publish or share ingested text beyond what your Logos licenses allow. Wheels and
source distributions contain no Logos content, sessions, browser profiles or
ingest checkpoints.

## Development

```bash
uv sync
uv run pytest tests/unit -q
```

<!-- pre-release:sdk -->
Until `marginalia-ai-sdk` 0.6.0 is published, this checkout resolves the SDK
from `vendor/marginalia-ai-sdk`, a copy of it declared in `pyproject.toml`
under `[tool.uv.sources]`. See
[vendor/README.md](vendor/README.md); the release workflow refuses to publish
until it is gone.
<!-- /pre-release:sdk -->

- **Unit tests** use the SDK only; core is never installed for them.
- **Core integration tests** need Research Engine 0.6 installed and a disposable
  PostgreSQL with `vector`, `pg_trgm` and `ltree`. They drive core's own
  discovery, approval, migration and load, and ingest a fixture book through it:
  `LOGOS_TEST_DB_URL=postgresql://... pytest tests/integration/test_core_plugin_host.py`.
- **Migration tests** need a disposable PostgreSQL:
  `LOGOS_TEST_DB_URL=postgresql://... uv run pytest tests/integration/test_plugin_migrations.py`.
  They create and drop their own databases, so never point them at a corpus you
  care about.
- **Live tests** call the real Logos API with a stored session and are opt-in:
  `LOGOS_LIVE=1 uv run pytest -m live`.
- **Artifacts:** `uv build`, then `python scripts/check_dist.py dist`.

## Security and support

- **Security issues:** do not put details in a public issue. Open an issue
  asking for a private contact, and the maintainer will arrange one.
- **Bugs and questions:** [GitHub issues](https://github.com/John-Cusack/marginalia-plugin-logos/issues).
  Include `logos-diagnose` output, which reports cookie names and lengths but
  never values. Never paste `cookies.json` or anything from `browser-profile/`.

Licensed under the [Apache License 2.0](https://github.com/John-Cusack/marginalia-plugin-logos/blob/main/LICENSE).
