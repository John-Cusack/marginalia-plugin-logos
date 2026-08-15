"""Cookie + 401 retry orchestration, silent re-login, and session keeping.

The in-memory cache is mtime-aware: if the cookies file on disk is newer than
the cached jar, the next ``get_cookie_jar()`` reloads transparently. That means
a fresh login from the CLI (which writes the file) is picked up by a long-running
MCP server without a restart.

Refresh is **silent-first and non-destructive**. ``refresh_auth()`` obtains a
fresh session via password-free renewal from the persistent SSO profile and only
replaces the stored session on success — so a failed refresh never wipes a still-
working ``cookies.json`` (the previous design's "clear then re-login" could, and
did, lose a freshly-captured jar). Interactive fallback only happens in CLI
contexts (``interactive=True``), never inside the running server.

``reload_cookies()`` drops the in-memory cache only. ``logout()`` is the
destructive full-clear (drops cache *and* deletes the file). Keep them separate.
"""

from __future__ import annotations

import asyncio
import time

from logos.lib.constants import COOKIE_PATH
from logos.lib.logger import log, log_error
from logos.lib.types import LogosCookieJar

from .cookie_store import clear_cookie, load_cookies, save_cookies
from .credentials import get_credentials
from .playwright_login import (
    LoginError,
    NeedsInteractiveLogin,
    interactive_login,
    silent_renew,
)
from .verify import check_session

_cached_jar: LogosCookieJar | None = None
_cached_mtime: float = 0.0
_refresh_lock: asyncio.Lock | None = None

# How close to expiry (seconds) before the session keeper proactively re-logs in.
_EXPIRY_REFRESH_MARGIN = 2 * 24 * 60 * 60  # 2 days
# How often the background keeper verifies the session.
_KEEPER_INTERVAL = 30 * 60  # 30 minutes

# --- Renewal coordination -------------------------------------------------
# A browser-backed renewal is expensive (spawns headless Chromium) and there is
# exactly one shared SSO profile, so renewals are serialized through
# _refresh_lock. These guards keep that serialization from turning into wasted
# work or a renewal storm:
#
#   * Coalesce: a caller that wins the lock just after another renewal completed
#     reuses that result instead of launching a second browser.
#   * Min proactive interval: a near-expiry signal can only drive a proactive
#     renewal this often, so a short-lived cookie can't trigger one every cycle.
#   * Silent backoff: after a renewal definitively fails (SSO gone), further
#     non-interactive attempts are suppressed for a window, so a dead session
#     doesn't spin up Chromium on every request.
#
# Timestamps are monotonic seconds (time.monotonic), never wall-clock — they are
# only compared against each other and must be immune to clock adjustments.
_RENEWAL_COALESCE_WINDOW = 30.0  # seconds
_MIN_PROACTIVE_RENEWAL_INTERVAL = 6 * 60 * 60.0  # 6 hours
_SILENT_BACKOFF = 5 * 60.0  # 5 minutes
_last_renewal_monotonic: float | None = None
_silent_backoff_until: float | None = None


def _file_mtime() -> float | None:
    try:
        return COOKIE_PATH.stat().st_mtime
    except FileNotFoundError:
        return None


def get_cookie_jar() -> LogosCookieJar | None:
    """Return the current cookie jar, reloading from disk if the file is newer."""
    global _cached_jar, _cached_mtime
    mtime = _file_mtime()
    if mtime is None:
        _cached_jar = None
        _cached_mtime = 0.0
        return None
    if _cached_jar is None or mtime > _cached_mtime:
        _cached_jar = load_cookies()
        _cached_mtime = mtime
    return _cached_jar


def get_cookie_header() -> str | None:
    """Return a Cookie header string for app.logos.com, or None if no jar."""
    jar = get_cookie_jar()
    return jar.header_value() if jar else None


def _store_jar(jar: LogosCookieJar) -> None:
    """Persist a freshly-obtained jar and sync the in-memory cache + mtime."""
    global _cached_jar, _cached_mtime
    save_cookies(jar)
    _cached_jar = jar
    new_mtime = _file_mtime()
    _cached_mtime = new_mtime if new_mtime is not None else 0.0


def _get_refresh_lock() -> asyncio.Lock:
    """Lazily create the refresh lock bound to the running loop."""
    global _refresh_lock
    if _refresh_lock is None:
        _refresh_lock = asyncio.Lock()
    return _refresh_lock


async def _interactive_login_with_creds() -> LogosCookieJar:
    """Open the one-time interactive login, pre-filling stored credentials."""
    creds = get_credentials()
    return await interactive_login(
        prefill_username=creds[0] if creds else None,
        prefill_password=creds[1] if creds else None,
    )


async def refresh_auth(*, interactive: bool = True) -> LogosCookieJar:
    """Obtain a fresh session, silent-first and non-destructively.

    Tries password-free **silent renewal** first (using the persistent SSO
    browser profile) — this is unattended and never hits reCAPTCHA. If the SSO
    session itself is gone, falls back to a one-time interactive login, but only
    when ``interactive`` is True (CLI contexts) — never inside the running
    server, where opening a browser would be useless/headless.

    A fresh session is obtained FIRST; the stored session is only replaced on
    success, so a failed refresh never destroys a still-working session.

    Concurrent callers are serialized through a single lock and *coalesced*: a
    caller that acquires the lock just after another renewal succeeded reuses
    that result rather than launching a second browser. While the SSO session is
    known-dead, non-interactive callers are short-circuited for a backoff window
    so an outage doesn't spawn Chromium on every request.
    """
    global _last_renewal_monotonic, _silent_backoff_until
    async with _get_refresh_lock():
        now = time.monotonic()

        # Coalesce: another caller (or the keeper) renewed while we waited for
        # the lock — hand back that fresh session instead of renewing again.
        if (
            _last_renewal_monotonic is not None
            and (now - _last_renewal_monotonic) < _RENEWAL_COALESCE_WINDOW
            and _cached_jar is not None
        ):
            return _cached_jar

        # Suppress silent attempts during a known SSO outage so we don't spin up
        # a headless browser (and wait ~30s) on every request until it recovers.
        if (
            not interactive
            and _silent_backoff_until is not None
            and now < _silent_backoff_until
        ):
            raise NeedsInteractiveLogin(
                "Silent renewal suppressed: SSO session needs re-seeding. "
                "Run 'logos-login'."
            )

        try:
            jar = await silent_renew()
        except NeedsInteractiveLogin:
            if not interactive:
                # Definitive failure — back off further silent attempts.
                _silent_backoff_until = time.monotonic() + _SILENT_BACKOFF
                raise
            log("Silent renewal unavailable; opening interactive login…")
            jar = await _interactive_login_with_creds()

        _store_jar(jar)
        _last_renewal_monotonic = time.monotonic()
        _silent_backoff_until = None  # fresh session — clear any outage backoff
        return jar


def reload_cookies() -> None:
    """Drop the in-memory cache. Next ``get_cookie_jar()`` re-reads from disk.

    Non-destructive — the cookies file is left intact. Use this when callers
    suspect the disk file has been updated out-of-band (CLI re-login, manual
    file write, etc.) and they want the running process to pick it up.
    """
    global _cached_jar, _cached_mtime
    _cached_jar = None
    _cached_mtime = 0.0


def logout() -> None:
    """Full logout: clear in-memory cache *and* delete the cookies file."""
    reload_cookies()
    clear_cookie()


def _near_expiry(jar: LogosCookieJar) -> bool:
    expiry = jar.min_auth_expiry()
    return expiry is not None and (expiry - time.time()) < _EXPIRY_REFRESH_MARGIN


def _should_proactively_renew(jar: LogosCookieJar) -> bool:
    """Whether the keeper should renew *ahead of* a lapse this cycle.

    True only when the session is near expiry AND we haven't already renewed
    within ``_MIN_PROACTIVE_RENEWAL_INTERVAL``. The interval guard matters when
    an auth cookie carries a short real expiry: ``_near_expiry`` would otherwise
    stay True forever and renew every keeper cycle. Bounding proactive renewals
    keeps that to once per interval; a session that actually goes invalid is
    still caught immediately by the keeper's ``check_session`` branch.
    """
    if not _near_expiry(jar):
        return False
    if _last_renewal_monotonic is None:
        return True
    return (time.monotonic() - _last_renewal_monotonic) >= _MIN_PROACTIVE_RENEWAL_INTERVAL


# ---------------------------------------------------------------------------
# Background session keeper
# ---------------------------------------------------------------------------

_keeper_task: "asyncio.Task | None" = None


def ensure_session_keeper() -> None:
    """Start the background session keeper if not already running.

    Safe to call repeatedly and from any authenticated code path. Does nothing
    when called outside a running event loop. The keeper renews the session
    silently (no password) from the persistent SSO profile.
    """
    global _keeper_task
    if _keeper_task is not None and not _keeper_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _keeper_task = loop.create_task(_session_keeper_loop())
    log("Session keeper started (silent auto-refresh enabled).")


async def _session_keeper_loop() -> None:
    """Periodically verify the session and silently renew before it lapses.

    The task is long-lived: it never exits on a failed renewal (only on
    cancellation), so ``ensure_session_keeper``'s done() guard prevents
    duplicate keepers. When the SSO session is gone it keeps retrying silently
    and self-recovers once the user re-seeds the profile via 'logos-login'; the
    "needs interactive" notice is logged once per outage, not every cycle.
    """
    announced_outage = False
    while True:
        try:
            await asyncio.sleep(_KEEPER_INTERVAL)
            jar = get_cookie_jar()
            if jar is None:
                log("Session keeper: no session, attempting silent renewal…")
                await refresh_auth(interactive=False)
            elif _should_proactively_renew(jar):
                log("Session keeper: cookie near expiry, renewing…")
                await refresh_auth(interactive=False)
            elif (await check_session(jar)) is False:
                # Only a DEFINITIVE negative (isAuthenticated=false / 401 / 403)
                # triggers renewal. A None (network blip, 5xx) is left alone so a
                # transient error can't churn a headless browser on a healthy
                # session every cycle.
                log("Session keeper: session invalid, renewing…")
                await refresh_auth(interactive=False)
            # Reached only when the iteration completed without raising —
            # session is healthy or was renewed, so clear any outage notice.
            announced_outage = False
        except asyncio.CancelledError:
            raise
        except NeedsInteractiveLogin:
            if not announced_outage:
                log_error(
                    "Session keeper: SSO session expired — run 'logos-login' to "
                    "re-seed it. Will keep retrying silently in the background."
                )
                announced_outage = True
        except LoginError as exc:
            log_error(f"Session keeper: renewal failed: {exc}")
        except Exception as exc:
            log_error(f"Session keeper: unexpected error: {exc}")


async def verify_auth(jar: LogosCookieJar | None = None) -> dict:
    """Verify authentication by calling ``/api/app/me``.

    Returns a dict with ``authenticated`` (bool) and, when authenticated,
    ``email`` and ``alias``. Used by the ``logos.auth_status`` / ``logos.diagnose``
    MCP tools. (The keeper uses the lighter tri-state
    ``auth.verify.check_session`` instead, so it can tell a real logout from a
    transient network failure.)
    """
    import httpx

    from logos.lib.constants import BASE_URL

    if jar is None:
        jar = get_cookie_jar()
    if jar is None:
        return {"authenticated": False, "error": "No cookies found"}

    url = f"{BASE_URL}/api/app/me"
    headers = {
        "Cookie": jar.header_value(),
        "Accept": "application/json",
        "Origin": "https://app.logos.com",
        "Referer": "https://app.logos.com/",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                return {"authenticated": False, "status_code": resp.status_code}
            data = resp.json()
            return {
                "authenticated": data.get("isAuthenticated", False),
                "email": data.get("email", ""),
                "alias": data.get("alias", ""),
            }
    except Exception as e:
        return {"authenticated": False, "error": str(e)}
