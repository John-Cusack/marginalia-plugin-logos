"""asyncpg connection pool.

Resolves the DSN in this priority order:
1. ``RE_DB_URL`` env var
2. ``DATABASE_URL`` env var
3. ``_DEFAULT_DSN`` — mirrors the core engine's default
   (``research_engine.config.settings.db_url``)

The default lets the plugin work out-of-the-box against the standard
local Docker Postgres without requiring env wiring in every MCP server
config. Production deployments should always set ``RE_DB_URL`` explicitly.
"""

from __future__ import annotations

import os

import asyncpg

from logos.lib.logger import log

# Mirror of research_engine.config.settings.db_url, with the SQLAlchemy
# ``+asyncpg`` driver suffix stripped — asyncpg.connect() rejects it.
_DEFAULT_DSN = "postgresql://re_dev:re_dev_pass@localhost:5435/research_engine"

_pool: asyncpg.Pool | None = None


def _get_dsn() -> str:
    """Return the resolved DSN, normalised to plain ``postgresql://`` form."""
    dsn = (
        os.environ.get("RE_DB_URL")
        or os.environ.get("DATABASE_URL")
        or _DEFAULT_DSN
    )
    # Core engine settings use SQLAlchemy-style URLs (``postgresql+asyncpg://``);
    # asyncpg only accepts plain ``postgresql://``. Strip any driver prefix.
    return dsn.replace("postgresql+asyncpg://", "postgresql://")


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            _get_dsn(),
            min_size=1,
            max_size=5,
        )
        log("Database connection pool opened")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        log("Database connection pool closed")
