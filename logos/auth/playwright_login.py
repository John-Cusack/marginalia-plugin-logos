"""Headed Chromium login via Playwright for Faithlife SSO.

Polls ``/api/app/me`` from the browser context to detect when sign-in has
actually completed. Avoids the previous design's two failure modes: blocking
``input()`` on stdin (unscriptable, can't run from a background task) and
cookie-name presence checks (Faithlife sets ``auth2`` as an anonymous
session cookie *before* login, so any presence-only check captures the wrong
state).
"""

from __future__ import annotations

import asyncio
import time

from logos.lib.constants import BASE_URL
from logos.lib.logger import log
from logos.lib.types import LogosCookie, LogosCookieJar

# Sign-in is human-paced (MFA, password manager, etc.). 10 minutes is generous
# enough for any reasonable flow, short enough that a stuck terminal is obvious.
LOGIN_TIMEOUT_S = 600.0
POLL_INTERVAL_S = 3.0


def _to_jar(cookies: list[dict]) -> LogosCookieJar:
    return LogosCookieJar(
        cookies=[
            LogosCookie(
                name=c["name"],
                value=c["value"],
                domain=c["domain"],
                path=c["path"],
                expires=c.get("expires", -1),
            )
            for c in cookies
        ]
    )


async def playwright_login() -> LogosCookieJar:
    """Open a headed Chromium, wait for the user to sign in, return cookies.

    Detects completion by polling ``/api/app/me`` for ``isAuthenticated: true``
    — the same predicate the server uses, so the flow survives any future
    cookie-name rotations without code changes.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is required for browser login. "
            "Install with: pip install playwright && playwright install chromium"
        ) from exc

    log("Opening browser for Logos login…")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto(f"{BASE_URL}/app")
        log(f"Sign in at {BASE_URL}/app — this script auto-detects completion.")

        deadline = time.monotonic() + LOGIN_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                resp = await context.request.get(
                    f"{BASE_URL}/api/app/me",
                    headers={"Accept": "application/json"},
                )
                if resp.ok:
                    data = await resp.json()
                    if data.get("isAuthenticated"):
                        log(f"Signed in as {data.get('alias', '?')} <{data.get('email', '?')}>")
                        cookies = await context.cookies()
                        await browser.close()
                        return _to_jar(cookies)
            except Exception:
                # Network blips, JSON decode errors, etc. — keep polling.
                pass
            await asyncio.sleep(POLL_INTERVAL_S)

        await browser.close()
        raise RuntimeError(
            f"Sign-in timed out after {int(LOGIN_TIMEOUT_S)}s. "
            "Did you complete the login flow in the opened browser?"
        )
