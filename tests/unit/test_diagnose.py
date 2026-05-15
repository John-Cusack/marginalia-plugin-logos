"""Tests for the logos.diagnose MCP tool.

The whole point of this tool is to make every failure mode legible. Each
test fixes a different failure mode (no file, expired/anonymous cookies,
unreachable server) and asserts the response surfaces enough information
to localize it.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def cookies_file(tmp_path, monkeypatch):
    path = tmp_path / "cookies.json"
    monkeypatch.setattr("logos.lib.constants.COOKIE_PATH", path)
    monkeypatch.setattr("logos.auth.manager.COOKIE_PATH", path)
    monkeypatch.setattr("logos.auth.cookie_store.COOKIE_PATH", path)
    monkeypatch.setattr("logos.auth.cookie_store.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("logos.auth.diagnose.COOKIE_PATH", path)

    from logos.auth import manager
    manager._cached_jar = None
    manager._cached_mtime = 0.0
    yield path
    manager._cached_jar = None
    manager._cached_mtime = 0.0


def _write_jar(path, cookies):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cookies": cookies}))


def _cookie(name, value="x", domain="app.logos.com"):
    return {"name": name, "value": value, "domain": domain, "path": "/", "expires": -1}


@pytest.mark.asyncio
async def test_no_file(cookies_file):
    from logos.auth.diagnose import run_diagnose
    with patch("logos.auth.diagnose.verify_auth",
               new=AsyncMock(return_value={"authenticated": False, "error": "No cookies found"})):
        result = await run_diagnose()
    assert result["file"]["exists"] is False
    assert result["jar"] is None
    assert result["live_check"]["authenticated"] is False


@pytest.mark.asyncio
async def test_anonymous_session_captured(cookies_file):
    """Captures the historical Bug 2: file present with auth2 but server rejects."""
    _write_jar(cookies_file, [_cookie("auth2", "anonymous-token" * 10)])
    from logos.auth.diagnose import run_diagnose
    with patch("logos.auth.diagnose.verify_auth",
               new=AsyncMock(return_value={"authenticated": False, "status_code": 200})):
        result = await run_diagnose()
    assert result["file"]["exists"] is True
    assert result["jar"]["count"] == 1
    assert result["jar"]["has_auth2"] is True
    assert result["jar"]["auth_cookie_name"] == "auth2"
    # File exists + jar has auth2 + live_check says false → diagnostic clearly
    # points at "the auth2 we have is not a valid session".
    assert result["live_check"]["authenticated"] is False


@pytest.mark.asyncio
async def test_healthy(cookies_file):
    _write_jar(cookies_file, [
        _cookie("auth2", "x" * 307),
        _cookie("auth-services", "y" * 154, domain="auth.faithlife.com"),
    ])
    from logos.auth.diagnose import run_diagnose
    with patch("logos.auth.diagnose.verify_auth",
               new=AsyncMock(return_value={
                   "authenticated": True, "alias": "John", "email": "john@example.com",
               })):
        result = await run_diagnose()
    assert result["jar"]["count"] == 2
    assert result["jar"]["has_auth2"] is True
    assert result["jar"]["auth_cookie_value_len"] == 307
    assert result["live_check"]["authenticated"] is True
    assert result["live_check"]["alias"] == "John"


@pytest.mark.asyncio
async def test_legacy_auth_cookie_only(cookies_file):
    """A jar with only the legacy `auth` cookie — surfaced as has_auth_legacy."""
    _write_jar(cookies_file, [_cookie("auth", "legacy")])
    from logos.auth.diagnose import run_diagnose
    with patch("logos.auth.diagnose.verify_auth",
               new=AsyncMock(return_value={"authenticated": False, "status_code": 200})):
        result = await run_diagnose()
    assert result["jar"]["has_auth2"] is False
    assert result["jar"]["has_auth_legacy"] is True
    assert result["jar"]["auth_cookie_name"] == "auth"


@pytest.mark.asyncio
async def test_server_unreachable(cookies_file):
    _write_jar(cookies_file, [_cookie("auth2", "x" * 307)])
    from logos.auth.diagnose import run_diagnose
    with patch("logos.auth.diagnose.verify_auth",
               new=AsyncMock(return_value={"authenticated": False, "error": "Connection refused"})):
        result = await run_diagnose()
    # Jar looks healthy but live_check.error surfaces the network failure.
    assert result["jar"]["has_auth2"] is True
    assert result["live_check"]["authenticated"] is False
    assert "Connection refused" in result["live_check"]["error"]
