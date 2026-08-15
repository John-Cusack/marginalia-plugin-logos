"""Persistent cookie storage at ~/.logos-mcp/cookies.json."""

from __future__ import annotations

import json
import time

from logos.lib.constants import CONFIG_DIR, COOKIE_PATH
from logos.lib.logger import log, log_debug
from logos.lib.types import LogosCookie, LogosCookieJar


def load_cookies() -> LogosCookieJar | None:
    try:
        if not COOKIE_PATH.exists():
            log_debug("No cookie file found")
            return None
        data = json.loads(COOKIE_PATH.read_text())
        if "cookies" in data:
            jar = LogosCookieJar(**data)
        else:
            jar = LogosCookieJar(cookies=[LogosCookie(**data)])
        auth = jar.auth_cookie
        if auth and auth.expires > 0 and time.time() > auth.expires:
            log("Cookie expired, clearing")
            clear_cookie()
            return None
        return jar
    except Exception:
        log_debug("Failed to load cookies")
        return None


def save_cookies(jar: LogosCookieJar) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    COOKIE_PATH.write_text(jar.model_dump_json(indent=2))
    log(f"Cookie saved to {COOKIE_PATH} ({len(jar.cookies)} cookies)")


def clear_cookie() -> None:
    try:
        if COOKIE_PATH.exists():
            COOKIE_PATH.unlink()
            log("Cookie cleared")
    except Exception:
        pass
