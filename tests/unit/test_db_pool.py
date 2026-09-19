"""Tests for the asyncpg DSN resolution in logos.db.pool.

Pins explicit, core-context, and standalone-environment precedence, driver
normalisation, missing-configuration failure, and credential redaction.
"""

from __future__ import annotations

import pytest
from research_engine_sdk import PluginConfigError, PluginContext


@pytest.fixture(autouse=True)
def _clear_db_sources(monkeypatch):
    """Each test gets a clean context and environment."""
    from logos.lib.context import reset_context

    reset_context()
    monkeypatch.delenv("RE_DB_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    yield
    reset_context()


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

    def test_refuses_missing_configuration(self):
        from logos.db.pool import _get_dsn

        with pytest.raises(PluginConfigError, match="no database URL"):
            _get_dsn()

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

    def test_core_context_beats_the_environment(self, tmp_path, monkeypatch):
        from logos.db.pool import _get_dsn
        from logos.lib.context import bind_context

        monkeypatch.setenv("RE_DB_URL", "postgresql://environment@h/d")
        bind_context(
            PluginContext(
                plugin_id="logos",
                data_dir=tmp_path,
                distribution_name="marginalia-ai-plugin-logos",
                distribution_version="0.2.1",
                database_url="postgresql+asyncpg://core:secret@h/core",
            )
        )

        assert _get_dsn() == "postgresql://core:secret@h/core"


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
