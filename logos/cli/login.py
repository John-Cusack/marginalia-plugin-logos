"""logos-login — sign in / refresh the Logos session.

    logos-login            One-time interactive login. Opens a browser, you sign
                           in (clearing reCAPTCHA / MFA), and the persistent SSO
                           profile is seeded so future renewals run silently.
    logos-login --refresh  Password-free silent renewal from the SSO profile.
                           No browser window, no captcha. Use this to top up a
                           lapsing session unattended.
    logos-login --status   Report the current session state without changing it.
    logos-login --migrate-data [--from PATH] [--dry-run] [--keep-source]
                           Move a session saved by a release before 0.2.0
                           (~/.logos-mcp) into the plugin data directory.

The session lives in the plugin data directory, the same one the engine hands
the plugin: $RE_DATA_DIR/plugin-data/logos, or ~/.research-engine/plugin-data/logos.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from logos.auth.cookie_store import load_cookies
from logos.auth.credentials import get_credentials
from logos.auth.manager import _store_jar, refresh_auth, verify_auth
from logos.auth.migrate_data import legacy_session_hint, migrate_data
from logos.auth.playwright_login import NeedsInteractiveLogin, silent_renew
from logos.lib.constants import cookie_path


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
    print(f"Session file: {cookie_path()}")
    jar = load_cookies()
    if jar is None:
        if hint := legacy_session_hint():
            print(f"Stored session: none. {hint}")
        else:
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="logos-login",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--refresh", action="store_true",
        help="password-free silent renewal from the SSO profile (no browser)",
    )
    group.add_argument(
        "--status", action="store_true",
        help="report current session state without changing it",
    )
    group.add_argument(
        "--migrate-data", action="store_true",
        help="move a pre-0.2.0 session from ~/.logos-mcp into the plugin data directory",
    )
    parser.add_argument(
        "--from", dest="source", type=Path, metavar="PATH",
        help="with --migrate-data: the old directory (default ~/.logos-mcp)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="with --migrate-data: report what would move, change nothing",
    )
    parser.add_argument(
        "--keep-source", action="store_true",
        help="with --migrate-data: leave the originals after a verified copy",
    )
    args = parser.parse_args(argv)

    if not args.migrate_data and (args.source or args.dry_run or args.keep_source):
        parser.error("--from, --dry-run and --keep-source need --migrate-data")
    if args.migrate_data:
        try:
            return migrate_data(
                args.source, dry_run=args.dry_run, keep_source=args.keep_source
            ).exit_code
        except OSError as exc:
            print(f"Migration failed: {exc.strerror or type(exc).__name__}", file=sys.stderr)
            return 1

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
