"""Shared constants."""

from pathlib import Path

BASE_URL = "https://app.logos.com"

CONFIG_DIR = Path.home() / ".logos-mcp"
COOKIE_PATH = CONFIG_DIR / "cookies.json"
# Persistent Chromium profile that holds the Faithlife SSO session, enabling
# password-free, captcha-free silent OAuth renewal of the app cookies.
BROWSER_PROFILE_DIR = CONFIG_DIR / "browser-profile"

DEFAULT_BIBLE_VERSION = "LEB"
DEFAULT_PASSAGE = "bible.62.3.16-bible.62.3.16"  # John 3:16

RESOURCE_TYPES: list[str] = [
    "commentary",
    "studynote",
    "crossreference",
    "atlas",
    "media",
]
