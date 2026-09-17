"""Where the session lives: the plugin data directory, resolved the way core does.

Inside the engine that is the bound `PluginContext.data_dir`. Outside it — the
``logos-login`` and ``logos-diagnose`` console scripts — it is
``$RE_DATA_DIR/plugin-data/logos``, falling back to
``~/.research-engine/plugin-data/logos``. A login run from a terminal must write
exactly where the running server reads, and neither side may need core to know.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from research_engine_sdk import PluginContext

from logos.lib.constants import browser_profile_dir, cookie_path, ensure_private_dir
from logos.lib.context import bind_context, reset_context

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    reset_context()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    from logos.auth import manager
    manager.reload_cookies()
    yield
    manager.reload_cookies()
    reset_context()


def _context(data_dir: Path) -> PluginContext:
    return PluginContext(
        plugin_id="logos",
        data_dir=data_dir,
        distribution_name="marginalia-ai-plugin-logos",
        distribution_version="0.2.0",
    )


def test_outside_core_the_data_dir_follows_re_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("RE_DATA_DIR", str(tmp_path / "re"))

    assert cookie_path() == tmp_path / "re" / "plugin-data" / "logos" / "cookies.json"
    assert browser_profile_dir() == tmp_path / "re" / "plugin-data" / "logos" / "browser-profile"


def test_without_re_data_dir_it_is_core_s_default(tmp_path, monkeypatch):
    monkeypatch.delenv("RE_DATA_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    expected = tmp_path / ".research-engine" / "plugin-data" / "logos" / "cookies.json"
    assert cookie_path() == expected
    assert ".logos-mcp" not in str(cookie_path())


def test_a_bound_context_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("RE_DATA_DIR", str(tmp_path / "env"))
    bind_context(_context(tmp_path / "from-core"))

    assert cookie_path() == tmp_path / "from-core" / "cookies.json"


def test_paths_are_resolved_per_call_not_at_import(tmp_path, monkeypatch):
    monkeypatch.setenv("RE_DATA_DIR", str(tmp_path / "a"))
    first = cookie_path()
    monkeypatch.setenv("RE_DATA_DIR", str(tmp_path / "b"))

    assert cookie_path() != first


async def test_the_auth_status_tool_reads_the_context_core_passes(tmp_path, monkeypatch):
    """No session at the context's directory: the tool says so, and says where.

    No network: with no jar, verification stops before any request.
    """
    monkeypatch.setenv("RE_DATA_DIR", str(tmp_path / "not-this-one"))
    from logos.tools.auth_status import handler

    result = json.loads(await handler(context=_context(tmp_path / "core")))

    assert result["authenticated"] is False
    assert result["cookie_path"] == str(tmp_path / "core" / "cookies.json")


def test_private_dir_is_0700_and_leaves_existing_parents_alone(tmp_path):
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o755)
    os.chmod(parent, 0o755)

    created = ensure_private_dir(parent / "plugin-data" / "logos")

    assert created.stat().st_mode & 0o777 == 0o700
    assert (parent / "plugin-data").stat().st_mode & 0o777 == 0o700
    assert parent.stat().st_mode & 0o777 == 0o755


def test_console_scripts_resolve_the_same_place_without_core(tmp_path):
    """Run as the installed script would: a fresh interpreter, no engine imported."""
    env = {**os.environ, "RE_DATA_DIR": str(tmp_path / "re"), "HOME": str(tmp_path / "home")}
    env.pop("PYTHONPATH", None)
    probe = (
        "import sys\n"
        "import logos.cli.login, logos.cli.diagnose\n"
        "from logos.lib.constants import cookie_path\n"
        "print(cookie_path())\n"
        "engine = [m for m in sys.modules if m == 'research_engine' or m.startswith('research_engine.')]\n"
        "assert not engine, engine\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], env=env, cwd=tmp_path,
        capture_output=True, text=True, check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == str(tmp_path / "re" / "plugin-data" / "logos" / "cookies.json")


def test_login_status_names_the_session_file(tmp_path):
    """`--status` with no session exits 2 before any request is made."""
    env = {**os.environ, "RE_DATA_DIR": str(tmp_path / "re"), "HOME": str(tmp_path / "home")}
    for key in ("LOGOS_USERNAME", "LOGOS_EMAIL", "LOGOS_PASSWORD", "LOGOS_ENV_FILE"):
        env.pop(key, None)
    done = subprocess.run(
        [sys.executable, "-m", "logos.cli.login", "--status"], env=env, cwd=tmp_path,
        capture_output=True, text=True, check=False,
    )
    assert done.returncode == 2, done.stderr
    assert str(tmp_path / "re" / "plugin-data" / "logos" / "cookies.json") in done.stdout
