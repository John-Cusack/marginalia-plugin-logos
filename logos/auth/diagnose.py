"""Diagnostic logic — pure Python, no MCP/research_engine dependencies.

Lives here (not in ``logos/tools/diagnose.py``) so the CLI entry point can
import it from a venv that doesn't have ``research_engine`` installed.
``logos.tools.diagnose`` is the MCP-tool wrapper that delegates here.
"""

from __future__ import annotations

import time
from typing import Any

from logos.lib.constants import COOKIE_PATH

from .manager import get_cookie_jar, verify_auth


async def run_diagnose() -> dict[str, Any]:
    """Return a single dict that surfaces every auth-layer state.

    Keys:
    - ``file``: file existence, mtime, age, size — distinguishes "no cookies yet"
      from "stale cookies".
    - ``jar``: cookie count, names, known-auth presence flags, auth value length —
      distinguishes "missing auth cookie" from "anonymous/empty auth cookie".
    - ``cache_age_s``: seconds since the in-memory cache was last refreshed —
      sanity check for the mtime-reload behaviour.
    - ``live_check``: parsed response from ``/api/app/me`` — the ground truth.
    """
    # Imported lazily so the manager's module globals reflect the latest state.
    from logos.auth import manager

    now = time.time()

    file_info: dict[str, Any] = {"exists": COOKIE_PATH.exists()}
    if file_info["exists"]:
        st = COOKIE_PATH.stat()
        file_info["mtime"] = st.st_mtime
        file_info["size_bytes"] = st.st_size
        file_info["age_s"] = now - st.st_mtime

    jar = get_cookie_jar()
    jar_info: dict[str, Any] | None = None
    if jar is not None:
        auth = jar.auth_cookie
        jar_info = {
            "count": len(jar.cookies),
            "names": sorted({c.name for c in jar.cookies}),
            "has_auth2": any(c.name == "auth2" and c.value for c in jar.cookies),
            "has_auth_legacy": any(c.name == "auth" and c.value for c in jar.cookies),
            "auth_cookie_name": auth.name if auth else None,
            "auth_cookie_value_len": len(auth.value) if auth else 0,
        }

    cache_age_s = (now - manager._cached_mtime) if manager._cached_mtime else None
    live = await verify_auth()

    return {
        "file": file_info,
        "jar": jar_info,
        "cache_age_s": cache_age_s,
        "live_check": live,
    }
