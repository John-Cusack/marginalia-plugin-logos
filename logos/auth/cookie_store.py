"""Persistent cookie storage at ``<plugin data dir>/cookies.json``."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from logos.lib.constants import cookie_path, ensure_private_dir
from logos.lib.logger import log, log_debug
from logos.lib.types import LogosCookie, LogosCookieJar


def parse_cookie_file(path: Path) -> LogosCookieJar:
    """Read *path* into a jar. Raises on anything that is not a stored session.

    Accepts both shapes this plugin has written: ``{"cookies": [...]}`` and the
    single-cookie object from before jars existed.
    """
    data = json.loads(path.read_text())
    if "cookies" in data:
        return LogosCookieJar(**data)
    return LogosCookieJar(cookies=[LogosCookie(**data)])


def load_cookies() -> LogosCookieJar | None:
    path = cookie_path()
    try:
        if not path.exists():
            log_debug("No cookie file found")
            return None
        jar = parse_cookie_file(path)
        auth = jar.auth_cookie
        if auth and auth.expires > 0 and time.time() > auth.expires:
            log("Cookie expired, clearing")
            clear_cookie()
            return None
        return jar
    except Exception:
        log_debug("Failed to load cookies")
        return None


def write_private_file(path: Path, content: str) -> None:
    """Write *content* to *path* as ``0600``, atomically.

    The running server reloads this file when its mtime moves, so it must never
    observe a half-written jar; a rename is the only write it cannot.
    """
    ensure_private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def save_cookies(jar: LogosCookieJar) -> None:
    path = cookie_path()
    write_private_file(path, jar.model_dump_json(indent=2))
    log(f"Cookie saved to {path} ({len(jar.cookies)} cookies)")


def clear_cookie() -> None:
    try:
        path = cookie_path()
        if path.exists():
            path.unlink()
            log("Cookie cleared")
    except Exception:
        pass
