"""Tests for the asyncpg DSN resolution in logos.db.pool.

Pins the resolution order — explicit DSN, RE_DB_URL, DATABASE_URL, then a
default that mirrors the core engine so the plugin works on a stock dev box
without env wiring — plus stripping of the SQLAlchemy
``+asyncpg`` suffix wherever it appears, and that a DSN can be printed without
its password.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_db_env(monkeypatch):
    """Each test gets a clean env — no inherited DSN from the shell."""
    monkeypatch.delenv("RE_DB_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)


class TestDsnResolution:
    def test_uses_re_db_url_when_set(self, monkeypatch):
        from logos.db.pool import _get_dsn
        monkeypatch.setenv("RE_DB_URL", "postgresql://u:p@h:1234/d")
        assert _get_dsn() == "postgresql://u:p@h:1234/d"

    def test_falls_back_to_database_url(self, monkeypatch):
        from logos.db.pool import _get_dsn
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:1234/d")
        assert _get_dsn() == "postgresql://u:p@h:1234/d"

    def test_re_db_url_takes_priority(self, monkeypatch):
        from logos.db.pool import _get_dsn
        monkeypatch.setenv("RE_DB_URL", "postgresql://winner@h/d")
        monkeypatch.setenv("DATABASE_URL", "postgresql://loser@h/d")
        assert _get_dsn() == "postgresql://winner@h/d"

    def test_falls_back_to_default_when_unset(self):
        from logos.db.pool import _DEFAULT_DSN, _get_dsn
        assert _get_dsn() == _DEFAULT_DSN
        # Sanity-check the default points at the standard local Docker Postgres
        # (must stay aligned with the core engine's default db_url).
        assert _DEFAULT_DSN.startswith("postgresql://")
        assert "localhost:5435" in _DEFAULT_DSN

    def test_strips_sqlalchemy_driver_suffix(self, monkeypatch):
        """Core uses postgresql+asyncpg://; asyncpg.connect rejects that form."""
        from logos.db.pool import _get_dsn
        monkeypatch.setenv("RE_DB_URL", "postgresql+asyncpg://u:p@h:1234/d")
        assert _get_dsn() == "postgresql://u:p@h:1234/d"

    def test_an_explicit_dsn_beats_the_environment(self, monkeypatch):
        """The migration entries pass core's database_url explicitly."""
        from logos.db.pool import resolve_dsn
        monkeypatch.setenv("RE_DB_URL", "postgresql://env@h/d")
        assert (
            resolve_dsn("postgresql+asyncpg://core@h/d") == "postgresql://core@h/d"
        )


class TestRedaction:
    def test_password_is_hidden(self):
        from logos.db.pool import redact_dsn
        redacted = redact_dsn("postgresql://re_dev:s3cr3t@localhost:5435/research_engine")
        assert "s3cr3t" not in redacted
        assert redacted == "postgresql://re_dev:***@localhost:5435/research_engine"

    def test_query_string_password_is_hidden(self):
        from logos.db.pool import redact_dsn
        redacted = redact_dsn("postgresql://h/d?user=u&password=s3cr3t&sslmode=require")
        assert "s3cr3t" not in redacted
        assert "sslmode=require" in redacted

    def test_a_dsn_without_a_password_is_unchanged(self):
        from logos.db.pool import redact_dsn
        assert redact_dsn("postgresql://u@h:1/d") == "postgresql://u@h:1/d"
