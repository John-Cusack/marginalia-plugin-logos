"""Quiet, non-navigating check of whether a cookie jar is authenticated.

Used by the background session keeper and silent-renewal flow, which need a
boolean answer without driving a browser page. (``manager.verify_auth`` returns
the richer dict the MCP ``auth_status``/``diagnose`` tools surface to users.)
Depends only on lib + httpx, so both manager and playwright_login can import it
without an import cycle.
"""

from __future__ import annotations

import httpx

from logos.lib.constants import BASE_URL
from logos.lib.logger import log_error
from logos.lib.types import LogosCookieJar

ME_URL = f"{BASE_URL}/api/app/me"
_HEADERS = {
    "Accept": "application/json",
    "Origin": "https://app.logos.com",
    "Referer": "https://app.logos.com/",
}


async def check_session(jar: LogosCookieJar, *, announce: bool = False) -> bool | None:
    """Tri-state auth check against /api/app/me.

    Returns:
      * ``True``  — authenticated.
      * ``False`` — *definitively* not authenticated (HTTP 200 with
        ``isAuthenticated: false``, or a 401/403 rejection).
      * ``None``  — could not determine (network error, timeout, 5xx, bad
        body). Callers MUST NOT treat ``None`` as a logout — the session may
        be perfectly healthy and the check just failed transiently. This is
        what stops the keeper from churning a headless browser on a blip.

    ``announce=True`` prints the signed-in identity (for CLI feedback); otherwise
    the check is quiet and only logs errors to stderr.
    """
    headers = {"Cookie": jar.header_value(), **_HEADERS}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(ME_URL, headers=headers)
    except Exception as exc:
        # Transient/network failure — unknown, NOT a definitive negative.
        if announce:
            print(f"Could not verify auth: {exc}")
        else:
            log_error(f"Session verification error: {exc}")
        return None

    if resp.status_code in (401, 403):
        if announce:
            print(f"API returned HTTP {resp.status_code} for /api/app/me")
        return False
    if resp.status_code != 200:
        # Server-side hiccup (5xx, etc.) — treat as unknown, not a logout.
        if announce:
            print(f"API returned HTTP {resp.status_code} for /api/app/me")
        return None
    try:
        data = resp.json()
    except Exception as exc:
        if announce:
            print(f"Could not parse /api/app/me response: {exc}")
        else:
            log_error(f"Session verification parse error: {exc}")
        return None

    authenticated = bool(data.get("isAuthenticated", False))
    if announce:
        if authenticated:
            print(f"Authenticated as: {data.get('email') or data.get('alias')}")
        else:
            print(f"API reports isAuthenticated=false (email={data.get('email', '')})")
    return authenticated


async def verify_session(jar: LogosCookieJar, *, announce: bool = False) -> bool:
    """Boolean convenience wrapper over :func:`check_session`.

    Collapses the "unknown" (``None``) state to ``False``. Use this only where a
    plain yes/no is wanted; the keeper uses ``check_session`` directly so it can
    distinguish "not authenticated" from "couldn't check".
    """
    return (await check_session(jar, announce=announce)) is True
