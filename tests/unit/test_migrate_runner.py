"""The migration runner's pure half: discovery, checksums, status arithmetic, CLI.

Applying migrations needs Postgres and lives in
``tests/integration/test_plugin_migrations.py``.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

import logos.db.migrate as migrate
from logos.db.migrate import (
    ADVISORY_LOCK_KEY,
    AppliedMigration,
    MigrationError,
    MigrationStatus,
    compute_status,
    discover_migrations,
)

pytestmark = pytest.mark.unit

MIGRATIONS_DIR = Path(migrate.__file__).parent / "migrations"
MANIFEST = Path(migrate.__file__).parents[1] / "plugin.yaml"


def _applied(revision: int, checksum: str) -> AppliedMigration:
    return AppliedMigration(
        revision=revision, name="x", checksum=checksum, applied_at=datetime.now(UTC)
    )


class TestDiscovery:
    def test_the_packaged_set_is_revision_one(self):
        migrations = discover_migrations()
        assert [(m.revision, m.name) for m in migrations] == [(1, "initial")]

    def test_target_revision_equals_the_number_of_files(self):
        files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        assert len(discover_migrations()) == len(files) == 1

    def test_checksum_is_the_sha256_of_the_file_bytes(self):
        (migration,) = discover_migrations()
        expected = hashlib.sha256((MIGRATIONS_DIR / "001_initial.sql").read_bytes()).hexdigest()
        assert migration.checksum == expected

    def test_checksum_is_stable_across_reads(self):
        assert [m.checksum for m in discover_migrations()] == [
            m.checksum for m in discover_migrations()
        ]

    def test_a_gap_is_refused(self, tmp_path):
        (tmp_path / "001_a.sql").write_text("SELECT 1;")
        (tmp_path / "003_c.sql").write_text("SELECT 1;")
        with pytest.raises(MigrationError, match="without gaps"):
            discover_migrations(tmp_path)

    def test_a_repeated_revision_is_refused(self, tmp_path):
        (tmp_path / "001_a.sql").write_text("SELECT 1;")
        (tmp_path / "001_b.sql").write_text("SELECT 2;")
        with pytest.raises(MigrationError):
            discover_migrations(tmp_path)

    def test_a_badly_named_file_is_refused_not_skipped(self, tmp_path):
        (tmp_path / "1_initial.sql").write_text("SELECT 1;")
        with pytest.raises(MigrationError, match="NNN_name"):
            discover_migrations(tmp_path)

    def test_non_sql_files_are_ignored(self, tmp_path):
        (tmp_path / "001_a.sql").write_text("SELECT 1;")
        (tmp_path / "README.md").write_text("notes")
        assert [m.revision for m in discover_migrations(tmp_path)] == [1]

    def test_manifest_declares_the_packaged_revision(self):
        if not MANIFEST.exists():
            pytest.skip("logos/plugin.yaml not written yet")
        import yaml

        database = yaml.safe_load(MANIFEST.read_text())["provides"]["database"]
        assert database["current_revision"] == len(discover_migrations())
        assert database["status_entry"] == "logos.db.migrate:status"
        assert database["upgrade_entry"] == "logos.db.migrate:upgrade"


class TestMigrationContent:
    @pytest.mark.parametrize("path", sorted(MIGRATIONS_DIR.glob("*.sql")), ids=lambda p: p.name)
    def test_no_destructive_statements(self, path):
        sql = re.sub(r"--[^\n]*", "", path.read_text())
        found = re.findall(r"\b(DROP|TRUNCATE|DELETE|RENAME)\b", sql, flags=re.IGNORECASE)
        assert not found, f"{path.name} contains {found}"

    def test_initial_adopts_an_existing_schema(self):
        """Every CREATE is IF NOT EXISTS, so a lazily-created database is adopted."""
        sql = re.sub(r"--[^\n]*", "", (MIGRATIONS_DIR / "001_initial.sql").read_text())
        creates = re.findall(r"CREATE\s+(?:TABLE|INDEX)\s+(?!IF NOT EXISTS)", sql)
        assert not creates

    def test_initial_creates_every_table_the_plugin_queries(self):
        sql = (MIGRATIONS_DIR / "001_initial.sql").read_text()
        for table in (
            "logos_scholars", "logos_authority", "logos_resources", "logos_api_calls",
            "logos_ingest_progress", "logos_ingest_chunks", "logos_ingest_article_texts",
            "logos_schema_migrations",
        ):
            assert f"CREATE TABLE IF NOT EXISTS {table} (" in sql, table


class TestComputeStatus:
    def test_an_empty_ledger_is_revision_zero_with_everything_pending(self):
        state = compute_status([], discover_migrations())
        assert (state.current_revision, state.target_revision) == (0, 1)
        assert state.pending == [1] and state.drift == []
        assert not state.up_to_date

    def test_a_matching_ledger_is_up_to_date(self):
        (m,) = discover_migrations()
        state = compute_status([_applied(1, m.checksum)], [m])
        assert state.current_revision == 1 and state.pending == [] and state.up_to_date

    def test_a_changed_checksum_is_drift(self):
        state = compute_status([_applied(1, "0" * 64)], discover_migrations())
        assert state.drift == [1] and not state.up_to_date

    def test_a_drifted_revision_is_not_current(self):
        """So core sees the database behind the manifest and calls `upgrade`,
        which refuses, instead of recording a drifted database as migrated."""
        state = compute_status([_applied(1, "0" * 64)], discover_migrations())
        assert state.current_revision == 0 and state.pending == []

    def test_a_recorded_revision_with_no_file_is_drift(self):
        (m,) = discover_migrations()
        state = compute_status([_applied(1, m.checksum), _applied(2, "f" * 64)], [m])
        assert state.drift == [2] and state.current_revision == 1


class TestCoreReport:
    """What core's `plugin migrate` reads: `current_revision` and `status`."""

    def test_pending(self):
        report = compute_status([], discover_migrations()).report()
        assert (report["current_revision"], report["status"]) == (0, "pending")
        json.dumps(report)

    def test_ok(self):
        (m,) = discover_migrations()
        report = compute_status([_applied(1, m.checksum)], [m]).report()
        assert (report["current_revision"], report["status"]) == (1, "ok")
        assert report["applied"][0]["checksum"] == m.checksum
        json.dumps(report)

    def test_drift(self):
        report = compute_status([_applied(1, "0" * 64)], discover_migrations()).report()
        assert (report["current_revision"], report["status"]) == (0, "drift")

    async def test_the_entries_take_the_keywords_core_passes(self, monkeypatch):
        seen = []
        current = MigrationStatus(current_revision=1, target_revision=1)

        async def fake_read(dsn=None, *, migrations=None):
            seen.append(("status", dsn))
            return current

        async def fake_apply(dsn=None, *, migrations=None):
            seen.append(("upgrade", dsn))
            return current
        monkeypatch.setattr(migrate, "read_status", fake_read)
        monkeypatch.setattr(migrate, "apply_upgrade", fake_apply)
        url = "postgresql+asyncpg://u:p@h/d"

        assert (await migrate.status(context=None, database_url=url))["status"] == "ok"
        assert (await migrate.upgrade(context=None, database_url=url))[
            "current_revision"
        ] == 1
        assert seen == [("status", url), ("upgrade", url)]


def test_advisory_lock_key_is_derived_from_its_documented_name():
    digest = hashlib.sha256(b"research-engine-plugin:logos:migrations").digest()
    assert ADVISORY_LOCK_KEY == int.from_bytes(digest[:8], "big", signed=True)


class TestCli:
    def _status(self, *, pending: list[int]) -> MigrationStatus:
        return MigrationStatus(
            current_revision=1 - len(pending), target_revision=1, pending=pending
        )

    def test_status_prints_json_without_the_password(self, monkeypatch, capsys):
        seen = {}

        async def fake_status(dsn=None, *, migrations=None):
            seen["dsn"] = dsn
            return self._status(pending=[1])

        monkeypatch.setattr(migrate, "read_status", fake_status)
        code = migrate.main(["status", "--dsn", "postgresql://u:hunter2@h:5/d"])

        out = capsys.readouterr().out
        assert "hunter2" not in out
        payload = json.loads(out)
        assert payload["database"] == "postgresql://u:***@h:5/d"
        assert payload["pending"] == [1]
        assert code == 1
        assert seen["dsn"] == "postgresql://u:hunter2@h:5/d"

    def test_upgrade_exits_zero_when_up_to_date(self, monkeypatch, capsys):
        async def fake_upgrade(dsn=None, *, migrations=None):
            return self._status(pending=[])

        monkeypatch.setattr(migrate, "apply_upgrade", fake_upgrade)
        assert migrate.main(["upgrade", "--dsn", "postgresql://h/d"]) == 0
        assert json.loads(capsys.readouterr().out)["current_revision"] == 1

    def test_drift_refusal_exits_two(self, monkeypatch, capsys):
        async def refusing(dsn=None, *, migrations=None):
            raise migrate.MigrationDriftError("refusing to migrate")

        monkeypatch.setattr(migrate, "apply_upgrade", refusing)
        assert migrate.main(["upgrade", "--dsn", "postgresql://u:pw@h/d"]) == 2
        out = capsys.readouterr().out
        assert "refusing" in out and ":pw@" not in out
