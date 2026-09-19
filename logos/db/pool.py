"""asyncpg connection pool.

Resolves the DSN in this priority order (see :func:`resolve_dsn`):

1. an explicit DSN from the migration capability or standalone CLI;
2. the secret database URL MarginaliaAI 0.6.2 supplies in ``PluginContext``;
3. ``RE_DB_URL`` or ``DATABASE_URL`` for standalone use.

Core loads `.env` into its settings without mutating process environment. Reading only
``RE_DB_URL`` therefore sent tools to a local fallback database even while core itself
used another database. The scoped context is now the authoritative tool-time source.
"""

from __future__ import annotations

import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg
from research_engine_sdk import PluginConfigError

from logos.lib.context import current_context
from logos.lib.logger import log


def _context_dsn() -> str | None:
    context = current_context()
    if context is None or context.database_url is None:
        return None
    return context.database_url.get_secret_value()

_pool: asyncpg.Pool | None = None


def resolve_dsn(dsn: str | None = None) -> str:
    """Return the configured DSN, normalised to plain ``postgresql://`` form."""
    resolved = (
        dsn
        or _context_dsn()
        or os.environ.get("RE_DB_URL")
        or os.environ.get("DATABASE_URL")
    )
    if not resolved:
        raise PluginConfigError(
            "logos has no database URL. Run it through marginalia-ai>=0.6.2, "
            "pass --dsn, or export RE_DB_URL for standalone use."
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
