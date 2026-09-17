"""`logos-login --migrate-data`: moving a pre-0.2.0 session, and refusing to guess.

Every test builds its own old and new locations under tmp. Nothing here reads or
writes the real ``~/.logos-mcp``.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from logos.auth import migrate_data as md
from logos.cli.login import main as login_main
from logos.lib.context import reset_context

pytestmark = pytest.mark.unit

SENTINEL = "SENTINEL-COOKIE-VALUE-7f3a"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    reset_context()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RE_DATA_DIR", str(tmp_path / "re"))
    yield
    reset_context()


@pytest.fixture
def legacy(tmp_path) -> Path:
    """A pre-0.2.0 state directory, as a real one looks — including a neighbour."""
    root = tmp_path / "home" / ".logos-mcp"
    root.mkdir(parents=True)
    cookies = root / "cookies.json"
    cookies.write_text(json.dumps({"cookies": [
        {"name": "auth2", "value": SENTINEL, "domain": "app.logos.com", "path": "/", "expires": -1},
        {"name": "auth-services", "value": SENTINEL, "domain": "auth.faithlife.com",
         "path": "/", "expires": -1},
    ]}))
    os.chmod(cookies, 0o664)  # what the old store actually left on disk

    profile = root / "browser-profile"
    (profile / "Default" / "Network").mkdir(parents=True)
    (profile / "Default" / "Network" / "Cookies").write_bytes(SENTINEL.encode() * 50)
    os.chmod(profile / "Default" / "Network" / "Cookies", 0o644)
    (profile / "Local State").write_text('{"profile": {}}')
    os.symlink("Default", profile / "Last Profile")

    (root / "kindle-profile").mkdir()
    (root / "kindle-profile" / "keep.txt").write_text("not ours")
    return root


@pytest.fixture
def destination(tmp_path) -> Path:
    return tmp_path / "re" / "plugin-data" / "logos"


def _mode(path: Path) -> int:
    return path.lstat().st_mode & 0o777


def _no_leak(report: md.MigrationReport, capsys, caplog) -> None:
    captured = capsys.readouterr()
    for text in (captured.out, captured.err, caplog.text, "\n".join(report.messages)):
        assert SENTINEL not in text


def test_dry_run_reports_and_changes_nothing(legacy, destination, capsys, caplog):
    report = md.migrate_data(dry_run=True)

    assert report.status == "dry_run" and report.exit_code == 0
    assert not destination.exists()
    assert (legacy / "cookies.json").exists() and (legacy / "browser-profile").exists()
    by_name = {e.name: e for e in report.entries}
    assert by_name["cookies.json"].action == "copy"
    assert by_name["browser-profile"].files == 2  # regular files; the symlink is not counted
    _no_leak(report, capsys, caplog)


def test_migrates_verifies_and_cleans_up(legacy, destination, capsys, caplog):
    before = {
        "cookies": (legacy / "cookies.json").read_bytes(),
        "profile": md._manifest(legacy / "browser-profile"),
    }
    caplog.set_level(logging.DEBUG)

    report = md.migrate_data()

    assert report.status == "migrated" and report.exit_code == 0
    assert (destination / "cookies.json").read_bytes() == before["cookies"]
    assert md._manifest(destination / "browser-profile") == before["profile"]
    assert (destination / "browser-profile" / "Last Profile").is_symlink()

    assert _mode(destination / "cookies.json") == 0o600
    assert _mode(destination) == 0o700
    assert _mode(destination / "browser-profile" / "Default" / "Network") == 0o700
    assert _mode(destination / "browser-profile" / "Default" / "Network" / "Cookies") == 0o600

    assert not (legacy / "cookies.json").exists()
    assert not (legacy / "browser-profile").exists()
    assert (legacy / "kindle-profile" / "keep.txt").exists(), "a neighbour's state was touched"
    assert report.left_in_place == ["kindle-profile"]
    assert not list(destination.glob(".migrating-*"))
    _no_leak(report, capsys, caplog)


def test_the_migrated_session_is_the_one_the_plugin_reads(legacy, destination):
    md.migrate_data()

    from logos.auth import manager
    manager.reload_cookies()
    jar = manager.get_cookie_jar()
    assert jar is not None and jar.auth_cookie.name == "auth2"


def test_running_it_again_is_harmless(legacy, destination):
    assert md.migrate_data().status == "migrated"
    again = md.migrate_data()

    assert again.status == "nothing_to_migrate" and again.exit_code == 0


def test_keep_source_then_a_later_run_finishes_the_job(legacy, destination):
    assert md.migrate_data(keep_source=True).status == "migrated"
    assert (legacy / "cookies.json").exists()

    second = md.migrate_data()

    assert second.status == "already_migrated" and second.exit_code == 0
    assert not (legacy / "cookies.json").exists()
    assert (destination / "cookies.json").exists()


def test_a_different_destination_stops_everything(legacy, destination, capsys, caplog):
    destination.mkdir(parents=True)
    (destination / "cookies.json").write_text('{"cookies": []}')

    report = md.migrate_data()

    assert report.status == "refused" and report.exit_code == 1
    assert (destination / "cookies.json").read_text() == '{"cookies": []}'
    assert not (destination / "browser-profile").exists(), "copied part-way past a conflict"
    assert (legacy / "cookies.json").exists() and (legacy / "browser-profile").exists()
    assert any("already holds a different" in m for m in report.messages)
    _no_leak(report, capsys, caplog)


def test_a_different_profile_tree_is_a_conflict(legacy, destination):
    (destination / "browser-profile" / "Default").mkdir(parents=True)
    (destination / "browser-profile" / "Default" / "Other").write_text("x")

    assert md.migrate_data().status == "refused"
    assert (legacy / "browser-profile").exists()


def test_an_identical_destination_counts_as_migrated(legacy, destination):
    destination.mkdir(parents=True)
    (destination / "cookies.json").write_bytes((legacy / "cookies.json").read_bytes())

    report = md.migrate_data()

    assert report.status == "migrated"
    assert {e.name: e.action for e in report.entries} == {
        "cookies.json": "already_present", "browser-profile": "copy",
    }
    assert _mode(destination / "cookies.json") == 0o600


def test_an_unreadable_session_is_not_migrated(legacy, destination, capsys, caplog):
    (legacy / "cookies.json").write_text(f"not json {SENTINEL}")

    report = md.migrate_data()

    assert report.status == "refused" and report.exit_code == 1
    assert not destination.exists()
    _no_leak(report, capsys, caplog)


def test_a_profile_held_by_a_running_browser_is_refused(legacy, destination):
    os.symlink(f"{socket.gethostname()}-{os.getpid()}", legacy / "browser-profile" / "SingletonLock")

    report = md.migrate_data()

    assert report.status == "refused"
    assert any("in use" in m for m in report.messages)
    assert not destination.exists()


def test_a_stale_lock_from_a_crashed_browser_is_left_behind(legacy, destination):
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    os.symlink(f"{socket.gethostname()}-{gone.pid}", legacy / "browser-profile" / "SingletonLock")

    report = md.migrate_data()

    assert report.status == "migrated"
    assert not (destination / "browser-profile" / "SingletonLock").is_symlink()


def test_a_copy_that_does_not_verify_is_removed_and_the_source_kept(
    legacy, destination, monkeypatch
):
    real_copy = md._copy_into_place

    def corrupting(source: Path, target: Path) -> None:
        real_copy(source, target)
        if target.name == "browser-profile":
            (target / "Local State").write_text("tampered")

    monkeypatch.setattr(md, "_copy_into_place", corrupting)

    report = md.migrate_data()

    assert report.status == "verification_failed" and report.exit_code == 3
    assert not (destination / "cookies.json").exists()
    assert not (destination / "browser-profile").exists()
    assert (legacy / "cookies.json").exists() and (legacy / "browser-profile").exists()


def test_nothing_there_is_not_an_error(tmp_path):
    report = md.migrate_data(source=tmp_path / "never-existed")

    assert report.status == "nothing_to_migrate" and report.exit_code == 0


def test_cli_migrates_from_an_explicit_source(tmp_path, legacy, destination, capsys):
    moved = tmp_path / "elsewhere"
    legacy.rename(moved)

    assert login_main(["--migrate-data", "--from", str(moved), "--dry-run"]) == 0
    assert not destination.exists()
    assert login_main(["--migrate-data", "--from", str(moved)]) == 0
    assert (destination / "cookies.json").exists()

    captured = capsys.readouterr()
    assert SENTINEL not in captured.out + captured.err


def test_cli_rejects_migration_flags_without_migrate_data(capsys):
    with pytest.raises(SystemExit) as exit_info:
        login_main(["--dry-run"])
    assert exit_info.value.code == 2


def test_cli_exit_code_reports_a_refusal(legacy, destination):
    destination.mkdir(parents=True)
    (destination / "cookies.json").write_text("{}")

    assert login_main(["--migrate-data"]) == 1


async def test_auth_status_points_at_the_migration_without_doing_it(legacy, destination):
    from logos.tools.auth_status import handler

    output = await handler()

    result = json.loads(output)
    assert result["authenticated"] is False
    assert "logos-login --migrate-data" in result["hint"]
    assert SENTINEL not in output
    assert (legacy / "cookies.json").exists() and not destination.exists()
