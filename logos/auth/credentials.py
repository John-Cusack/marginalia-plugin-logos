"""Logos account credentials, used to pre-fill the one-time interactive login.

Silent renewal never needs these — it relies on the persistent SSO browser
profile, not a password. The credentials only make the manual first sign-in
trivial (email pre-filled).

Resolution order:

1. Real process environment — ``LOGOS_USERNAME`` (or ``LOGOS_EMAIL``) +
   ``LOGOS_PASSWORD`` — if something already exported them.
2. The **main Marginalia repo's ``.env``** file. The core engine loads its
   ``.env`` through pydantic-settings with an ``RE_`` prefix, so it never
   exports the un-prefixed ``LOGOS_*`` keys to ``os.environ`` — we read the
   file directly. Candidate locations, in order:
     - ``$LOGOS_ENV_FILE`` (explicit override), then
     - ``<cwd>/.env`` (the engine runs from the main repo root), then
     - the sibling ``MarginaliaAI/.env`` next to this plugin checkout.

The parser is dependency-free and handles quoted values / special characters,
so passwords containing shell metacharacters are read verbatim.
"""

from __future__ import annotations

import os
from pathlib import Path


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a ``KEY=value`` ``.env`` file into a dict. Tolerant, never raises
    on malformed lines; strips one layer of matching surrounding quotes."""
    values: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        values[key] = val
    return values


def _env_file_candidates() -> list[Path]:
    """Ordered, de-duplicated list of ``.env`` paths to consult."""
    candidates: list[Path] = []
    override = os.environ.get("LOGOS_ENV_FILE")
    if override:
        candidates.append(Path(override).expanduser())
    # The engine runs from the main repo root, where its .env lives.
    candidates.append(Path.cwd() / ".env")
    # Sibling main repo next to this plugin checkout
    # (.../repos/marginalia-plugin-logos/logos/auth/credentials.py).
    repos_dir = Path(__file__).resolve().parents[3]
    candidates.append(repos_dir / "MarginaliaAI" / ".env")

    seen: set[Path] = set()
    unique: list[Path] = []
    for path in candidates:
        resolved = path.expanduser()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def get_credentials() -> tuple[str, str] | None:
    """Return (username, password) from the environment or the main repo's
    ``.env``, or None if neither is fully available."""
    username = os.environ.get("LOGOS_USERNAME") or os.environ.get("LOGOS_EMAIL")
    password = os.environ.get("LOGOS_PASSWORD")
    if username and password:
        return username, password

    for path in _env_file_candidates():
        try:
            if not path.is_file():
                continue
            vals = _parse_env_file(path)
        except OSError:
            continue
        file_user = username or vals.get("LOGOS_USERNAME") or vals.get("LOGOS_EMAIL")
        file_pass = password or vals.get("LOGOS_PASSWORD")
        if file_user and file_pass:
            return file_user, file_pass

    return None


def credentials_available() -> bool:
    return get_credentials() is not None
