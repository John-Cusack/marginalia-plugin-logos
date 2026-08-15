"""Tests for the renewal coordinator and the tri-state session check.

These pin the behaviours added to harden the silent-renewal flow:

* ``refresh_auth`` coalesces back-to-back renewals (one browser, not two),
* a known SSO outage backs off non-interactive attempts (no per-request browser),
* a successful renewal clears that backoff,
* ``_should_proactively_renew`` rate-limits near-expiry renewals,
* ``check_session`` distinguishes "not authenticated" from "couldn't check".
"""

from __future__ import annotations

import time

import pytest

from logos.auth import manager, verify
from logos.auth.playwright_login import NeedsInteractiveLogin
from logos.lib.types import LogosCookie, LogosCookieJar


def _jar(expires: float = 10**12) -> LogosCookieJar:
    return LogosCookieJar(
        cookies=[
            LogosCookie(name="auth2", value="x", domain=".logos.com", path="/", expires=expires)
        ]
    )


@pytest.fixture(autouse=True)
def reset_coordinator():
    """Clear all renewal-coordinator module globals around each test."""
    for attr, val in (
        ("_cached_jar", None),
        ("_cached_mtime", 0.0),
        ("_refresh_lock", None),  # rebind the lock to each test's event loop
        ("_last_renewal_monotonic", None),
        ("_silent_backoff_until", None),
    ):
        setattr(manager, attr, val)
    yield
    for attr in ("_cached_jar", "_last_renewal_monotonic", "_silent_backoff_until"):
        setattr(manager, attr, None)


def _patch_store(monkeypatch):
    """Make _store_jar update the in-memory cache without touching disk."""
    monkeypatch.setattr(
        manager, "_store_jar", lambda jar: setattr(manager, "_cached_jar", jar)
    )


async def test_coalesces_back_to_back_renewals(monkeypatch):
    calls = {"n": 0}

    async def fake_silent():
        calls["n"] += 1
        return _jar()

    monkeypatch.setattr(manager, "silent_renew", fake_silent)
    _patch_store(monkeypatch)

    j1 = await manager.refresh_auth(interactive=False)
    j2 = await manager.refresh_auth(interactive=False)

    assert calls["n"] == 1, "second renewal within the window should be coalesced"
    assert j1 is j2


async def test_silent_outage_backs_off(monkeypatch):
    calls = {"n": 0}

    async def fake_silent():
        calls["n"] += 1
        raise NeedsInteractiveLogin("SSO gone")

    monkeypatch.setattr(manager, "silent_renew", fake_silent)

    with pytest.raises(NeedsInteractiveLogin):
        await manager.refresh_auth(interactive=False)
    with pytest.raises(NeedsInteractiveLogin):
        await manager.refresh_auth(interactive=False)

    assert calls["n"] == 1, "second attempt during backoff must not launch a browser"


async def test_success_clears_backoff(monkeypatch):
    async def fake_silent():
        return _jar()

    monkeypatch.setattr(manager, "silent_renew", fake_silent)
    _patch_store(monkeypatch)

    # Pretend we're mid-outage; an interactive call bypasses suppression.
    manager._silent_backoff_until = time.monotonic() + 999
    await manager.refresh_auth(interactive=True)

    assert manager._silent_backoff_until is None


def test_should_proactively_renew_respects_interval():
    near = _jar(expires=time.time() + 60)  # within the 2-day margin

    manager._last_renewal_monotonic = None
    assert manager._should_proactively_renew(near) is True

    manager._last_renewal_monotonic = time.monotonic()  # just renewed
    assert manager._should_proactively_renew(near) is False

    manager._last_renewal_monotonic = (
        time.monotonic() - manager._MIN_PROACTIVE_RENEWAL_INTERVAL - 1
    )
    assert manager._should_proactively_renew(near) is True

    # A far-off expiry is never "near", regardless of last-renewal time.
    far = _jar(expires=time.time() + 10**7)
    manager._last_renewal_monotonic = None
    assert manager._should_proactively_renew(far) is False


class _FakeResp:
    def __init__(self, status, payload=None, raise_json=False):
        self.status_code = status
        self._payload = payload
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("bad json")
        return self._payload


class _FakeClient:
    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, *a, **k):
        if self._exc:
            raise self._exc
        return self._resp


@pytest.mark.parametrize(
    "resp, exc, expected",
    [
        (_FakeResp(200, {"isAuthenticated": True}), None, True),
        (_FakeResp(200, {"isAuthenticated": False}), None, False),
        (_FakeResp(401), None, False),  # definitive rejection
        (_FakeResp(403), None, False),
        (_FakeResp(503), None, None),  # server hiccup → unknown
        (None, RuntimeError("network"), None),  # transient → unknown
        (_FakeResp(200, raise_json=True), None, None),  # bad body → unknown
    ],
)
async def test_check_session_tristate(monkeypatch, resp, exc, expected):
    monkeypatch.setattr(
        verify.httpx, "AsyncClient", lambda *a, **k: _FakeClient(resp, exc)
    )
    assert await verify.check_session(_jar()) is expected
