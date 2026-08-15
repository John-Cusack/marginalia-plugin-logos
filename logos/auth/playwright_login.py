"""Logos/Faithlife auth via a persistent Chromium profile.

The Faithlife sign-in form is reCAPTCHA-protected, so unattended *password*
login from a headless browser gets scored as a bot and rejected. Instead we
keep a persistent browser profile (``BROWSER_PROFILE_DIR``) that holds the
long-lived Faithlife SSO cookie:

* :func:`interactive_login` — a one-time headed login (the user clears reCAPTCHA
  / MFA) that seeds the profile and captures the app session cookies. Completion
  is detected by polling ``/api/app/me`` for ``isAuthenticated: true`` — the same
  predicate the server uses — so the flow needs no blocking stdin and survives
  future cookie-name rotations.
* :func:`silent_renew` — headless, password-free, captcha-free renewal. As long
  as the SSO cookie in the profile is alive, navigating the sign-in endpoint
  silently re-issues fresh app cookies. This is how the keeper stays logged in.
"""

from __future__ import annotations

import asyncio
import time

from logos.lib.constants import BASE_URL, BROWSER_PROFILE_DIR
from logos.lib.logger import log
from logos.lib.types import LogosCookie, LogosCookieJar

# Navigating here 302s to the Faithlife OAuth authorize page. With a valid SSO
# cookie it bounces straight back to the app (silent renewal); otherwise it
# renders the email/password form.
SIGNIN_PATH = "/auth/signin"
ME_PATH = "/api/app/me"

# Sign-in is human-paced (MFA, password manager, reCAPTCHA). 10 minutes is
# generous enough for any reasonable flow, short enough that a stuck terminal
# is obvious.
LOGIN_TIMEOUT_S = 600.0
POLL_INTERVAL_S = 3.0

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]


class LoginError(RuntimeError):
    """A login attempt failed for an unexpected reason."""


class NeedsInteractiveLogin(LoginError):
    """Silent renewal failed because the SSO session is gone — a human must
    complete an interactive login (with reCAPTCHA) to re-seed the profile."""


def _jar_from_cookies(cookies: list[dict]) -> LogosCookieJar:
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


async def _context_authenticated(context) -> bool:
    """Authoritative auth check via the context's request API.

    Hits /api/app/me as a JSON XHR and reads ``isAuthenticated``. Unlike
    navigating the page to the endpoint and ``json.loads``-ing the rendered
    body, this does not depend on how the browser renders the response (JSON
    viewer chrome, HTML interstitial, redirect) and never disturbs whatever
    page is showing. Same predicate ``interactive_login`` polls on.
    """
    try:
        resp = await context.request.get(
            f"{BASE_URL}{ME_PATH}", headers={"Accept": "application/json"}
        )
        if not resp.ok:
            return False
        return bool((await resp.json()).get("isAuthenticated", False))
    except Exception:
        return False


def profile_seeded() -> bool:
    """True if the persistent SSO profile has actually been initialized by a
    prior browser launch (i.e. a login was at least attempted).

    Chromium creates a ``Default`` subdirectory on first launch of a persistent
    context. We use its presence as a cheap, dependency-free signal: when it is
    absent, no login has ever run, so a silent renewal would just waste ~30s
    spinning up a browser only to fail — callers should fast-fail instead.
    """
    return (BROWSER_PROFILE_DIR / "Default").is_dir()


def _import_playwright():
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is required for login. "
            "Install with: pip install playwright && playwright install chromium"
        ) from exc
    return async_playwright


async def _launch_profile_context(p, *, headless: bool, no_viewport: bool = False):
    """Open the persistent SSO-profile browser context, with a clear error.

    Chromium permits only one live persistent context per profile directory
    (a ``SingletonLock`` in the profile). Concurrent users of the shared SSO
    profile — e.g. the server's background keeper mid-renewal while someone runs
    ``logos-login`` — would otherwise fail with an opaque internal error. Catch
    that specific case and raise actionable guidance; re-raise anything else.
    """
    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    kwargs: dict = {
        "headless": headless,
        "args": _LAUNCH_ARGS,
        "user_agent": _USER_AGENT,
    }
    if no_viewport:
        kwargs["no_viewport"] = True
    try:
        return await p.chromium.launch_persistent_context(str(BROWSER_PROFILE_DIR), **kwargs)
    except Exception as exc:
        msg = str(exc)
        if "SingletonLock" in msg or "ProcessSingleton" in msg or "already in use" in msg.lower():
            raise LoginError(
                "The Logos browser profile is already in use by another process "
                "(the MCP server's session keeper, or a concurrent 'logos-login'). "
                "Only one process can hold the SSO profile at a time — stop the "
                "other and retry."
            ) from exc
        raise


async def _try_prefill(page, username: str | None, password: str | None) -> None:
    """Best-effort fill of the (two-step) sign-in form. Never raises."""
    try:
        if username and await page.locator("input#username").count():
            await page.fill("input#username", username)
        if (
            password
            and await page.locator("input#password").count()
            and await page.locator("input#password").is_visible()
        ):
            await page.fill("input#password", password)
    except Exception:
        pass


async def silent_renew(*, timeout_ms: int = 30_000) -> LogosCookieJar:
    """Refresh app cookies without a password using the persistent SSO profile.

    Raises :class:`NeedsInteractiveLogin` if the profile has no valid SSO session.
    """
    async_playwright = _import_playwright()
    log("Attempting silent session renewal (no password)…")
    async with async_playwright() as p:
        context = await _launch_profile_context(p, headless=True)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(f"{BASE_URL}{SIGNIN_PATH}", wait_until="domcontentloaded")
            # Let the OAuth bounce settle back to the app.
            try:
                await page.wait_for_url(
                    lambda url: "app.logos.com" in url and "/auth/" not in url,
                    timeout=timeout_ms,
                )
            except Exception:
                pass

            if not await _context_authenticated(context):
                raise NeedsInteractiveLogin(
                    "Silent renewal failed: no valid Faithlife SSO session in the "
                    "browser profile. Run 'logos-login' to log in interactively."
                )

            # The server has confirmed the session is authenticated, so the
            # captured cookies ARE the working session. Don't gate on a known
            # auth-cookie NAME here — that would wrongly fail a valid renewal if
            # Faithlife rotates the cookie name. Just warn so the rotation is
            # visible in logs.
            jar = _jar_from_cookies(await context.cookies())
            if not jar.auth_cookie:
                log("Silent renewal: authenticated but no known auth-cookie name "
                    "matched — capturing the session anyway (cookie name may have "
                    "rotated; consider updating _AUTH_COOKIE_NAMES).")
            log("Silent renewal succeeded.")
            return jar
        finally:
            await context.close()


async def interactive_login(
    prefill_username: str | None = None,
    prefill_password: str | None = None,
    *,
    timeout_s: float = LOGIN_TIMEOUT_S,
) -> LogosCookieJar:
    """One-time headed login that seeds the persistent SSO profile.

    Opens a visible browser, pre-fills the form when credentials are provided,
    and polls ``/api/app/me`` until sign-in actually completes — no blocking
    stdin. The persistent profile means subsequent renewals can run silently.
    Requires a display.
    """
    async_playwright = _import_playwright()
    log("Opening browser for Logos login…")
    async with async_playwright() as p:
        context = await _launch_profile_context(p, headless=False, no_viewport=True)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(f"{BASE_URL}{SIGNIN_PATH}", wait_until="domcontentloaded")

            await _try_prefill(page, prefill_username, prefill_password)

            log(f"Sign in at {BASE_URL} — this auto-detects completion.")
            if prefill_username:
                log("(Your email is pre-filled — enter your password and clear "
                    "the reCAPTCHA if prompted.)")

            # Poll via the context's request API so we do NOT navigate the
            # visible sign-in form away while the user is still on it.
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                try:
                    resp = await context.request.get(
                        f"{BASE_URL}{ME_PATH}", headers={"Accept": "application/json"}
                    )
                    if resp.ok:
                        data = await resp.json()
                        if data.get("isAuthenticated"):
                            log(f"Signed in as {data.get('alias', '?')} <{data.get('email', '?')}>")
                            # isAuthenticated is authoritative — capture and
                            # return the session even if no known auth-cookie
                            # name matched (cookie names can rotate). Warn rather
                            # than loop to the timeout reporting a false failure.
                            jar = _jar_from_cookies(await context.cookies())
                            if not jar.auth_cookie:
                                log("Signed in but no known auth-cookie name "
                                    "matched — capturing the session anyway.")
                            log(f"Captured {len(jar.cookies)} cookies.")
                            return jar
                except Exception:
                    # Network blips, JSON decode errors, etc. — keep polling.
                    pass
                await asyncio.sleep(POLL_INTERVAL_S)

            raise LoginError(
                f"Sign-in timed out after {int(timeout_s)}s. "
                "Did you complete the login flow in the opened browser?"
            )
        finally:
            await context.close()


# Backwards-compatible alias used elsewhere in the codebase.
async def playwright_login() -> LogosCookieJar:
    return await interactive_login()
