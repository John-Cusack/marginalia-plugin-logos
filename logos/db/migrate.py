"""Versioned migrations for the tables this plugin owns.

Core's schema migrations never touch ``logos_*`` tables; this module is their
only writer. The manifest declares :func:`status` and :func:`upgrade` as the
plugin's database entries. ``research-engine plugin migrate logos`` calls them
after the operator approves the plugin, as
``entry(context=..., database_url=...)``, and reads ``current_revision`` and
``status`` from the mapping they return. Tools never migrate: before 0.2.0 every
tool that touched the database ran ``CREATE TABLE IF NOT EXISTS`` on its first
call, so a schema change could land from whichever request happened to come
first, unreviewed.

Migrations are ``NNN_name.sql`` files in ``logos/db/migrations``, numbered from
001 without gaps, read as package resources so an installed wheel migrates
exactly what it shipped. Each applied revision is recorded in
``logos_schema_migrations`` with the SHA-256 of its file. A recorded checksum
that no longer matches — or a recorded revision whose file is gone — is drift,
and ``upgrade`` refuses to run past it rather than guess which schema is real.

Nothing here drops, truncates or deletes.

Outside core::

    python -m logos.db.migrate status [--dsn URL]
    python -m logos.db.migrate upgrade [--dsn URL]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from importlib.resources import files
from typing import TYPE_CHECKING, Any, Literal, Protocol

import asyncpg

from logos.db.pool import redact_dsn, resolve_dsn
from logos.lib.context import PLUGIN_ID
from logos.lib.logger import log

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from research_engine_sdk import PluginContext

LEDGER_TABLE = "logos_schema_migrations"

#: Session-level ``pg_advisory_lock`` key held for the whole of an upgrade, so
#: two processes upgrading one database run one after the other and the second
#: finds nothing pending. It is the first eight bytes of
#: ``sha256(b"research-engine-plugin:logos:migrations")`` as a signed int64:
#: fixed, so every process contends for the same lock, and derived from a name
#: so it cannot collide with another plugin's by accident.
ADVISORY_LOCK_KEY = -2457515912433562171

_FILENAME = re.compile(r"^(?P<revision>\d{3})_(?P<name>[a-z0-9_]+)\.sql$")


class MigrationError(RuntimeError):
    """The migration set, or the database's record of it, cannot be trusted."""


class MigrationDriftError(MigrationError):
    """An applied revision no longer matches the file this package ships."""


@dataclass(frozen=True)
class Migration:
    revision: int
    name: str
    sql: str
    checksum: str


@dataclass(frozen=True)
class AppliedMigration:
    """One row of the ledger."""

    revision: int
    name: str
    checksum: str
    applied_at: datetime


@dataclass(frozen=True)
class MigrationStatus:
    """The ledger compared with the migrations this package ships.

    ``current_revision`` counts only revisions that are applied *and* still match
    their file, contiguous from 1. A drifted revision therefore puts the database
    behind the manifest, and core's ``plugin migrate`` calls :func:`upgrade`,
    which refuses — rather than recording a drifted database as current.
    """

    current_revision: int
    target_revision: int
    applied: list[AppliedMigration] = field(default_factory=list)
    pending: list[int] = field(default_factory=list)
    drift: list[int] = field(default_factory=list)
    plugin_id: str = PLUGIN_ID

    @property
    def up_to_date(self) -> bool:
        return not self.pending and not self.drift

    @property
    def state(self) -> Literal["ok", "pending", "drift"]:
        if self.drift:
            return "drift"
        return "pending" if self.pending else "ok"

    def report(self) -> dict[str, Any]:
        """JSON-safe, in the shape core's ``plugin migrate`` reads."""
        return {
            "plugin_id": self.plugin_id,
            "status": self.state,
            "current_revision": self.current_revision,
            "target_revision": self.target_revision,
            "pending": list(self.pending),
            "drift": list(self.drift),
            "applied": [
                {
                    "revision": row.revision,
                    "name": row.name,
                    "checksum": row.checksum,
                    "applied_at": row.applied_at.isoformat(),
                }
                for row in self.applied
            ],
        }


class _Resource(Protocol):
    name: str

    def iterdir(self) -> Iterable[_Resource]: ...
    def read_bytes(self) -> bytes: ...
    def is_file(self) -> bool: ...


def discover_migrations(directory: _Resource | None = None) -> list[Migration]:
    """The packaged migrations in revision order, checked for gaps.

    *directory* defaults to ``logos/db/migrations`` as a package resource.
    """
    root = directory if directory is not None else files("logos.db").joinpath("migrations")
    found: list[Migration] = []
    for entry in root.iterdir():
        if not entry.name.endswith(".sql") or not entry.is_file():
            continue
        match = _FILENAME.match(entry.name)
        if match is None:
            raise MigrationError(
                f"migration file {entry.name!r} is not named NNN_name.sql"
            )
        data = entry.read_bytes()
        found.append(
            Migration(
                revision=int(match["revision"]),
                name=match["name"],
                sql=data.decode("utf-8"),
                checksum=hashlib.sha256(data).hexdigest(),
            )
        )
    found.sort(key=lambda m: m.revision)
    revisions = [m.revision for m in found]
    if revisions != list(range(1, len(found) + 1)):
        raise MigrationError(
            f"migration revisions must run 1..N without gaps or repeats, got {revisions}"
        )
    return found


def compute_status(
    applied: Sequence[AppliedMigration], migrations: Sequence[Migration]
) -> MigrationStatus:
    """Compare the ledger with the shipped migrations. Pure."""
    shipped = {m.revision: m for m in migrations}
    recorded = {row.revision: row for row in applied}
    drift = sorted(
        row.revision
        for row in applied
        if row.revision not in shipped or shipped[row.revision].checksum != row.checksum
    )
    current = 0
    while current + 1 in recorded and current + 1 in shipped and current + 1 not in drift:
        current += 1
    return MigrationStatus(
        current_revision=current,
        target_revision=max(shipped, default=0),
        applied=sorted(applied, key=lambda row: row.revision),
        pending=[m.revision for m in migrations if m.revision not in recorded],
        drift=drift,
    )


async def _read_ledger(conn: asyncpg.Connection) -> list[AppliedMigration]:
    """Applied revisions. A database without the ledger is at revision 0."""
    if not await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", LEDGER_TABLE):
        return []
    rows = await conn.fetch(
        f"SELECT revision, name, checksum, applied_at FROM {LEDGER_TABLE} ORDER BY revision"
    )
    return [AppliedMigration(**dict(row)) for row in rows]


async def read_status(
    dsn: str | None = None, *, migrations: Sequence[Migration] | None = None
) -> MigrationStatus:
    """Current and target revision, pending revisions and drift. Writes nothing."""
    shipped = list(migrations) if migrations is not None else discover_migrations()
    conn = await asyncpg.connect(resolve_dsn(dsn))
    try:
        return compute_status(await _read_ledger(conn), shipped)
    finally:
        await conn.close()


async def apply_upgrade(
    dsn: str | None = None, *, migrations: Sequence[Migration] | None = None
) -> MigrationStatus:
    """Apply every pending migration, each in its own transaction with its ledger row.

    Holds :data:`ADVISORY_LOCK_KEY` throughout and re-reads the ledger under it.
    Raises :class:`MigrationDriftError`, having applied nothing, if any recorded
    revision disagrees with the shipped files. *migrations* replaces the
    packaged set; it exists for tests.
    """
    shipped = list(migrations) if migrations is not None else discover_migrations()
    conn = await asyncpg.connect(resolve_dsn(dsn))
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", ADVISORY_LOCK_KEY)
        try:
            state = compute_status(await _read_ledger(conn), shipped)
            if state.drift:
                raise MigrationDriftError(
                    f"refusing to migrate: applied revision(s) {state.drift} do not "
                    f"match the migrations in this package. Restore the released "
                    f"migration files or investigate the database; nothing was applied."
                )
            for migration in shipped:
                if migration.revision not in state.pending:
                    continue
                async with conn.transaction():
                    await conn.execute(migration.sql)
                    await conn.execute(
                        f"INSERT INTO {LEDGER_TABLE} (revision, name, checksum) "
                        f"VALUES ($1, $2, $3)",
                        migration.revision,
                        migration.name,
                        migration.checksum,
                    )
                log(f"Logos database migrated to revision {migration.revision} "
                    f"({migration.name})")
            return compute_status(await _read_ledger(conn), shipped)
        finally:
            if not conn.is_closed():
                await conn.execute("SELECT pg_advisory_unlock($1)", ADVISORY_LOCK_KEY)
    finally:
        await conn.close()


async def status(
    *, context: PluginContext | None = None, database_url: str | None = None
) -> dict[str, Any]:
    """Manifest ``status_entry``: :func:`read_status` as the mapping core reads.

    Core passes the engine's own ``database_url``; *context* carries nothing the
    migrations need.
    """
    return (await read_status(database_url)).report()


async def upgrade(
    *, context: PluginContext | None = None, database_url: str | None = None
) -> dict[str, Any]:
    """Manifest ``upgrade_entry``: :func:`apply_upgrade` as the mapping core reads.

    Raises :class:`MigrationDriftError` on drift; core records the plugin as
    errored and does not load it.
    """
    return (await apply_upgrade(database_url)).report()


def main(argv: list[str] | None = None) -> int:
    """``python -m logos.db.migrate status|upgrade [--dsn URL]``.

    Prints the status as JSON on stdout. Exit status: 0 when the database is up
    to date, 1 when revisions are pending or drifted, 2 when upgrade refused.
    """
    import structlog

    previous = structlog.get_config()
    # Keep stdout for the JSON.
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))
    try:
        return _run_cli(argv)
    finally:
        structlog.configure(**previous)


def _run_cli(argv: list[str] | None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m logos.db.migrate",
        description="Report or apply the Logos plugin's database migrations.",
    )
    parser.add_argument("command", choices=["status", "upgrade"])
    parser.add_argument(
        "--dsn",
        help="PostgreSQL URL. Defaults to RE_DB_URL, then DATABASE_URL, then the "
        "engine's local default.",
    )
    args = parser.parse_args(argv)
    dsn = resolve_dsn(args.dsn)

    entry = apply_upgrade if args.command == "upgrade" else read_status
    try:
        result = asyncio.run(entry(dsn))
    except MigrationDriftError as exc:
        print(json.dumps({"database": redact_dsn(dsn), "error": str(exc)}, indent=2))
        return 2

    print(json.dumps({"database": redact_dsn(dsn), **result.report()}, indent=2))
    return 0 if result.up_to_date else 1


if __name__ == "__main__":
    sys.exit(main())
