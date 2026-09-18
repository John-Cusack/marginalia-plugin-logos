"""asyncpg connection pool.

Resolves the DSN in this priority order (see :func:`resolve_dsn`):

1. an explicit DSN, where the caller has one: the migration CLI's ``--dsn``, or
   the ``database_url`` core passes to the migration entries;
2. ``RE_DB_URL`` env var;
3. ``DATABASE_URL`` env var;
4. ``_DEFAULT_DSN`` — mirrors the core engine's default.

Research Engine 0.6 passes tool handlers no database URL: ``PluginContext`` has
none, and database access is not a manifest permission. Inside the engine the
tools therefore read the same ``RE_DB_URL`` the engine's process was started
with. A URL that exists only in the engine's ``.env`` file never reaches the
process environment, so the tools fall back to the default.
"""

from __future__ import annotations

import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg

from logos.lib.logger import log

# Mirror of the core engine's default db_url, with the SQLAlchemy ``+asyncpg``
# driver suffix stripped — asyncpg.connect() rejects it.
_DEFAULT_DSN = "postgresql://re_dev:re_dev_pass@localhost:5435/research_engine"

_pool: asyncpg.Pool | None = None


def resolve_dsn(dsn: str | None = None) -> str:
    """Return the DSN to connect with, normalised to plain ``postgresql://`` form."""
    resolved = (
        dsn
        or os.environ.get("RE_DB_URL")
        or os.environ.get("DATABASE_URL")
        or _DEFAULT_DSN
    )
    # Core engine settings use SQLAlchemy-style URLs (``postgresql+asyncpg://``);
    # asyncpg only accepts plain ``postgresql://``. Strip any driver prefix.
    return resolved.replace("postgresql+asyncpg://", "postgresql://")


def _get_dsn() -> str:
    """The DSN the shared pool opens with."""
    return resolve_dsn()


def redact_dsn(dsn: str) -> str:
    """*dsn* with any password replaced, safe to print or log."""
    parts = urlsplit(dsn)
    netloc = parts.netloc
    userinfo, at, hostport = netloc.rpartition("@")
    if at and ":" in userinfo:
        netloc = f"{userinfo.split(':', 1)[0]}:***@{hostport}"
    query = parts.query
    if query:
        query = urlencode(
            [(k, "***" if k.lower() == "password" else v) for k, v in parse_qsl(query)],
            safe="*",
        )
    return urlunsplit(parts._replace(netloc=netloc, query=query))


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
