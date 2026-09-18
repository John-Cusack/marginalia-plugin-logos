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
    from logos.lib.context import reset_context

    reset_context()
    monkeypatch.setenv("RE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = tmp_path / "plugin-data" / "logos" / "cookies.json"

    from logos.auth import manager
    manager.reload_cookies()
    yield path
    manager.reload_cookies()
    reset_context()


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
    assert result["file"]["path"] == str(cookies_file)
    assert result["jar"] is None
    assert result["legacy_session"] is None
    assert result["live_check"]["authenticated"] is False


@pytest.mark.asyncio
async def test_session_left_at_the_pre_0_2_location_is_named(cookies_file, tmp_path):
    """An upgrade leaves the session behind; say where, and how to move it."""
    legacy = tmp_path / "home" / ".logos-mcp" / "cookies.json"
    _write_jar(legacy, [_cookie("auth2", "SENTINEL-COOKIE-VALUE")])
    from logos.auth.diagnose import run_diagnose
    with patch("logos.auth.diagnose.verify_auth",
               new=AsyncMock(return_value={"authenticated": False})):
        result = await run_diagnose()

    assert result["file"]["exists"] is False
    hint = result["legacy_session"]["hint"]
    assert str(legacy) in hint
    assert "logos-login --migrate-data" in hint
    assert "SENTINEL-COOKIE-VALUE" not in json.dumps(result)
    assert legacy.exists(), "diagnosing must not migrate anything"


@pytest.mark.asyncio
async def test_no_legacy_hint_once_the_new_location_has_a_session(cookies_file, tmp_path):
    _write_jar(tmp_path / "home" / ".logos-mcp" / "cookies.json", [_cookie("auth2", "old")])
    _write_jar(cookies_file, [_cookie("auth2", "new")])
    from logos.auth.diagnose import run_diagnose
    with patch("logos.auth.diagnose.verify_auth",
               new=AsyncMock(return_value={"authenticated": True})):
        result = await run_diagnose()
    assert result["legacy_session"] is None


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


def test_help_does_not_run_the_diagnostic(capsys, monkeypatch):
    """`--help` is how CI proves the console script starts; it must not probe
    the session or the network."""
    import logos.cli.diagnose as cli

    async def _must_not_run():
        raise AssertionError("--help ran the diagnostic")

    monkeypatch.setattr(cli, "run_diagnose", _must_not_run)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])
    assert exit_info.value.code == 0
    assert "logos-diagnose" in capsys.readouterr().out
