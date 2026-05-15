"""Tests for the asyncpg DSN resolution in logos.db.pool.

Pins three guarantees: (1) RE_DB_URL wins over DATABASE_URL, (2) DATABASE_URL
is honoured when RE_DB_URL is absent, (3) the fallback default mirrors the
core engine so the plugin works on a stock dev box without env wiring, and
the SQLAlchemy ``+asyncpg`` suffix is stripped wherever it appears.
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
        # (must stay aligned with research_engine.config.settings.db_url).
        assert _DEFAULT_DSN.startswith("postgresql://")
        assert "localhost:5435" in _DEFAULT_DSN

    def test_strips_sqlalchemy_driver_suffix(self, monkeypatch):
        """Core uses postgresql+asyncpg://; asyncpg.connect rejects that form."""
        from logos.db.pool import _get_dsn
        monkeypatch.setenv("RE_DB_URL", "postgresql+asyncpg://u:p@h:1234/d")
        assert _get_dsn() == "postgresql://u:p@h:1234/d"
