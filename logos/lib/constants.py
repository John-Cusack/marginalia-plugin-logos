"""Shared constants, and where this plugin keeps its state.

State paths are functions rather than module constants. The data directory comes
from the `PluginContext` core binds on each call (see `logos.lib.context`), and
outside core — the console scripts, tests — from ``RE_DATA_DIR``. Either can
change after import, so a path computed at import time would be wrong.
"""

from __future__ import annotations

import os
from pathlib import Path

from logos.lib.context import data_dir

BASE_URL = "https://app.logos.com"

COOKIE_FILENAME = "cookies.json"
BROWSER_PROFILE_DIRNAME = "browser-profile"

DEFAULT_BIBLE_VERSION = "LEB"
DEFAULT_PASSAGE = "bible.62.3.16-bible.62.3.16"  # John 3:16

RESOURCE_TYPES: list[str] = [
    "commentary",
    "studynote",
    "crossreference",
    "atlas",
    "media",
]


def state_dir() -> Path:
    """This plugin's data directory: session cookies and the SSO browser profile."""
    return data_dir()


def cookie_path() -> Path:
    """The stored Logos session."""
    return state_dir() / COOKIE_FILENAME


def browser_profile_dir() -> Path:
    """Persistent Chromium profile that holds the Faithlife SSO session, enabling
    password-free, captcha-free silent OAuth renewal of the app cookies."""
    return state_dir() / BROWSER_PROFILE_DIRNAME


def legacy_state_dir() -> Path:
    """Where releases before 0.2.0 kept the session.

    Read only by ``logos-login --migrate-data`` and by the status hint that
    points at it. Nothing writes here.
    """
    return Path.home() / ".logos-mcp"


def ensure_private_dir(path: Path) -> Path:
    """Create *path* (and any missing parents) as ``0700``, and hold it there.

    Parents that already exist are left as they are — they may be core's.
    """
    missing = []
    probe = path
    while not probe.exists():
        missing.append(probe)
        if probe.parent == probe:
            break
        probe = probe.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path
