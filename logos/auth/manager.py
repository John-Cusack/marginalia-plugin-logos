"""Cookie + 401 retry orchestration.

In-memory cache is mtime-aware: if the cookies file on disk is newer than
the cached jar, the next ``get_cookie_jar()`` reloads transparently. That
means a fresh login from the CLI (which writes the file) is picked up by
a long-running MCP server without restart.

``reload_cookies()`` drops the in-memory cache only. ``logout()`` is the
destructive full-clear (drops cache *and* deletes the file). The two were
conflated before, which caused at least one accidental wipe of a freshly-
captured jar — keep them separate.
"""

from __future__ import annotations

from logos.lib.constants import COOKIE_PATH
from logos.lib.logger import log
from logos.lib.types import LogosCookieJar

from .cookie_store import clear_cookie, load_cookies, save_cookies
from .playwright_login import playwright_login

_cached_jar: LogosCookieJar | None = None
_cached_mtime: float = 0.0


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


async def refresh_auth() -> LogosCookieJar:
    """Run the full interactive login flow, persist + cache the new jar."""
    global _cached_jar, _cached_mtime
    log("Refreshing authentication…")
    clear_cookie()
    _cached_jar = None
    _cached_mtime = 0.0
    jar = await playwright_login()
    save_cookies(jar)
    _cached_jar = jar
    new_mtime = _file_mtime()
    _cached_mtime = new_mtime if new_mtime is not None else 0.0
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


async def verify_auth(jar: LogosCookieJar | None = None) -> dict:
    """Verify authentication by calling ``/api/app/me``.

    Returns a dict with ``authenticated`` (bool) and, when authenticated,
    ``email`` and ``alias``. Used by both the ``logos.auth_status`` /
    ``logos.diagnose`` MCP tools and the http client's 401-retry logic.
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
