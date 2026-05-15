"""Tests for the mtime-aware cookie jar cache + the reload/logout split.

The cache layer's whole purpose is to detect when ``cookies.json`` has been
rewritten out-of-band (e.g. by a CLI re-login) and serve the fresh jar
without requiring a process restart. These tests pin that behaviour.
"""

from __future__ import annotations

import json
import os
import time

import pytest


@pytest.fixture
def cookies_file(tmp_path, monkeypatch):
    """Redirect every reference to COOKIE_PATH at a tmp file and reset cache state."""
    path = tmp_path / "cookies.json"
    monkeypatch.setattr("logos.lib.constants.COOKIE_PATH", path)
    monkeypatch.setattr("logos.auth.manager.COOKIE_PATH", path)
    monkeypatch.setattr("logos.auth.cookie_store.COOKIE_PATH", path)
    monkeypatch.setattr("logos.auth.cookie_store.CONFIG_DIR", tmp_path)

    # Clear module-global cache between tests.
    from logos.auth import manager
    manager._cached_jar = None
    manager._cached_mtime = 0.0

    yield path

    manager._cached_jar = None
    manager._cached_mtime = 0.0


def _write_jar(path, cookies):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cookies": cookies}))


def _cookie(name, value="x", domain="app.logos.com", path="/", expires=-1):
    return {"name": name, "value": value, "domain": domain, "path": path, "expires": expires}


class TestMtimeAwareCache:
    def test_returns_none_when_file_missing(self, cookies_file):
        from logos.auth.manager import get_cookie_jar
        assert get_cookie_jar() is None

    def test_loads_on_first_call(self, cookies_file):
        _write_jar(cookies_file, [_cookie("auth2", "v1")])
        from logos.auth.manager import get_cookie_jar
        jar = get_cookie_jar()
        assert jar is not None
        assert jar.auth_cookie.value == "v1"

    def test_reuses_cache_when_file_unchanged(self, cookies_file):
        _write_jar(cookies_file, [_cookie("auth2", "v1")])
        from logos.auth.manager import get_cookie_jar
        jar1 = get_cookie_jar()
        jar2 = get_cookie_jar()
        assert jar1 is jar2  # cache hit — same object

    def test_reloads_when_file_is_newer(self, cookies_file):
        _write_jar(cookies_file, [_cookie("auth2", "v1")])
        from logos.auth.manager import get_cookie_jar

        jar1 = get_cookie_jar()
        assert jar1.auth_cookie.value == "v1"

        # Rewrite the file and bump mtime past the cached one.
        _write_jar(cookies_file, [_cookie("auth2", "v2")])
        future = time.time() + 10
        os.utime(cookies_file, (future, future))

        jar2 = get_cookie_jar()
        assert jar2 is not jar1
        assert jar2.auth_cookie.value == "v2"

    def test_drops_cache_when_file_disappears(self, cookies_file):
        _write_jar(cookies_file, [_cookie("auth2", "v1")])
        from logos.auth.manager import get_cookie_jar
        assert get_cookie_jar() is not None

        cookies_file.unlink()
        assert get_cookie_jar() is None

        # And it recovers if the file comes back.
        _write_jar(cookies_file, [_cookie("auth2", "v3")])
        future = time.time() + 10
        os.utime(cookies_file, (future, future))
        jar = get_cookie_jar()
        assert jar is not None
        assert jar.auth_cookie.value == "v3"


class TestReloadVsLogout:
    """The two were conflated before today's fix. Pin the new contract."""

    def test_reload_cookies_drops_cache_but_leaves_file(self, cookies_file):
        _write_jar(cookies_file, [_cookie("auth2", "v1")])
        from logos.auth import manager
        from logos.auth.manager import get_cookie_jar, reload_cookies

        get_cookie_jar()  # populate cache
        assert manager._cached_jar is not None

        reload_cookies()
        assert manager._cached_jar is None
        assert manager._cached_mtime == 0.0
        assert cookies_file.exists()  # file must NOT be deleted

        # Next call re-reads from disk transparently.
        jar = get_cookie_jar()
        assert jar.auth_cookie.value == "v1"

    def test_logout_drops_cache_and_deletes_file(self, cookies_file):
        _write_jar(cookies_file, [_cookie("auth2", "v1")])
        from logos.auth import manager
        from logos.auth.manager import get_cookie_jar, logout

        get_cookie_jar()
        assert manager._cached_jar is not None
        assert cookies_file.exists()

        logout()
        assert manager._cached_jar is None
        assert not cookies_file.exists()  # the destructive behaviour, kept explicit
