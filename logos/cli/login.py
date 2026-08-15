"""logos-login — sign in / refresh the Logos session.

    logos-login            One-time interactive login. Opens a browser, you sign
                           in (clearing reCAPTCHA / MFA), and the persistent SSO
                           profile is seeded so future renewals run silently.
    logos-login --refresh  Password-free silent renewal from the SSO profile.
                           No browser window, no captcha. Use this to top up a
                           lapsing session unattended.
    logos-login --status   Report the current session state without changing it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from logos.auth.cookie_store import load_cookies
from logos.auth.credentials import get_credentials
from logos.auth.manager import _store_jar, refresh_auth, verify_auth
from logos.auth.playwright_login import NeedsInteractiveLogin, silent_renew


async def _refresh() -> int:
    """Silent renewal only — never opens a browser."""
    try:
        jar = await silent_renew()
    except NeedsInteractiveLogin:
        print(
            "No valid SSO session in the browser profile. "
            "Run 'logos-login' to sign in interactively (one time).",
            file=sys.stderr,
        )
        return 1
    _store_jar(jar)
    print(f"Refreshed session ({len(jar.cookies)} cookies saved).")
    return 0 if (await verify_auth(jar)).get("authenticated") else 2


async def _status() -> int:
    creds = get_credentials()
    print(f"Credentials configured (LOGOS_USERNAME/PASSWORD): {'yes' if creds else 'no'}")
    jar = load_cookies()
    if jar is None:
        print("Stored session: none. Run 'logos-login' or 'logos-login --refresh'.")
        return 2
    logos_cookies = [c for c in jar.cookies if "logos.com" in c.domain.lower()]
    print(f"Stored cookies: {len(jar.cookies)} ({len(logos_cookies)} Logos-domain)")
    auth = jar.auth_cookie
    print(f"Auth cookie: {auth.name if auth else 'MISSING'}")
    expiry = jar.min_auth_expiry()
    if expiry:
        print(f"Auth cookie expiry: {(expiry - time.time()) / 86400:.1f} days from now")
    else:
        print("Auth cookie expiry: session (no fixed expiry)")
    result = await verify_auth(jar)
    if result.get("authenticated"):
        print(f"Status: AUTHENTICATED ({result.get('email') or result.get('alias')})")
        return 0
    print("Status: NOT authenticated (session likely invalidated server-side).")
    return 2


async def _login() -> int:
    """Interactive (silent-first) login: seeds the SSO profile."""
    jar = await refresh_auth(interactive=True)
    print(f"Saved {len(jar.cookies)} cookies.")
    return 0 if (await verify_auth(jar)).get("authenticated") else 2


def main() -> int:
    parser = argparse.ArgumentParser(prog="logos-login", description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--refresh", action="store_true",
        help="password-free silent renewal from the SSO profile (no browser)",
    )
    group.add_argument(
        "--status", action="store_true",
        help="report current session state without changing it",
    )
    args = parser.parse_args()

    if args.refresh:
        runner = _refresh
    elif args.status:
        runner = _status
    else:
        runner = _login

    try:
        return asyncio.run(runner())
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"Failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
