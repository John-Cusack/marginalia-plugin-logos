"""Move a pre-0.2.0 session from ``~/.logos-mcp`` into the plugin data directory.

Explicit, never automatic: ``logos-login --migrate-data``. The session is a
credential, so the rules are strict:

* Only ``cookies.json`` and ``browser-profile/`` move. Anything else in the old
  directory — other tools have kept state there — is left where it is.
* A destination that already holds something *different* stops the whole run
  before anything is copied. One that holds an identical copy counts as migrated.
* Copies land in a staging name and are renamed into place, so an interrupted run
  never leaves a half-written destination that the next run would call a conflict.
* Nothing is widened: ``cookies.json`` is ``0600``, directories ``0700``, and
  profile files keep their owner bits with group and other bits removed.
* The old files are removed only after the copies are verified — the jar parses,
  and the profile tree matches the source file for file.
* Cookie values are never printed or logged. Reports carry names of files, counts
  and sizes only.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import socket
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from logos.lib.constants import (
    BROWSER_PROFILE_DIRNAME,
    COOKIE_FILENAME,
    cookie_path,
    ensure_private_dir,
    legacy_state_dir,
    state_dir,
)

from .cookie_store import parse_cookie_file

#: Lock artefacts Chromium keeps at the top of a live profile. They describe a
#: running process, not the session, so they are neither copied nor compared.
_SINGLETONS = frozenset({"SingletonLock", "SingletonCookie", "SingletonSocket"})

#: ``relative path -> ("file", size, sha256) | ("link", target)``.
Manifest = dict[str, tuple]


class MigrationRefused(Exception):
    """The migration cannot proceed safely. Nothing was changed."""


@dataclass
class EntryReport:
    name: str
    #: ``missing``, ``file`` or ``dir``.
    kind: str
    files: int = 0
    bytes: int = 0
    #: ``copy``, ``already_present`` or ``absent``.
    action: str = "absent"


@dataclass
class MigrationReport:
    source: Path
    destination: Path
    entries: list[EntryReport] = field(default_factory=list)
    #: ``nothing_to_migrate``, ``dry_run``, ``migrated``, ``already_migrated``,
    #: ``refused`` or ``verification_failed``.
    status: str = "nothing_to_migrate"
    left_in_place: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return {"refused": 1, "verification_failed": 3}.get(self.status, 0)


def legacy_session_hint() -> str | None:
    """A pointer at ``--migrate-data`` when only the old location has a session."""
    legacy = legacy_state_dir() / COOKIE_FILENAME
    try:
        if cookie_path().exists() or not legacy.is_file():
            return None
    except OSError:
        return None
    return (
        f"No session at {cookie_path()}, but a session from an earlier release "
        f"exists at {legacy}. Run 'logos-login --migrate-data' to move it."
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _manifest(root: Path) -> Manifest:
    """What *root* contains, by content. A file's manifest has one entry, ``""``."""
    if root.is_symlink():
        return {"": ("link", os.readlink(root))}
    if root.is_file():
        return {"": ("file", root.stat().st_size, _sha256(root))}
    found: Manifest = {}
    for current, dirs, files in os.walk(root):
        base = Path(current)
        relative_dir = base.relative_to(root)
        for name in [*dirs, *files]:
            path = base / name
            relative = (relative_dir / name).as_posix()
            if relative in _SINGLETONS:
                continue
            if path.is_symlink():
                found[relative] = ("link", os.readlink(path))
            elif path.is_file():
                found[relative] = ("file", path.stat().st_size, _sha256(path))
            elif path.is_dir():
                found[relative] = ("dir",)
    return found


def _profile_in_use(profile: Path) -> bool:
    """Whether a Chromium on this host holds *profile* right now.

    ``SingletonLock`` is a symlink to ``<hostname>-<pid>``. A lock left by a
    crashed browser names a pid that is gone, and does not block the migration.
    """
    lock = profile / "SingletonLock"
    if not lock.is_symlink():
        return False
    host, _, pid = os.readlink(lock).rpartition("-")
    if host != socket.gethostname() or not pid.isdigit():
        return True  # another host, or unreadable: assume live
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _inventory(path: Path, name: str, manifest: Manifest) -> EntryReport:
    files = [entry for entry in manifest.values() if entry[0] == "file"]
    return EntryReport(
        name=name,
        kind="dir" if path.is_dir() else "file",
        files=len(files),
        bytes=sum(entry[1] for entry in files),
    )


def _tighten(root: Path) -> None:
    """Strip group/other bits everywhere under *root*; directories become 0700."""
    if root.is_file():
        os.chmod(root, 0o600)
        return
    os.chmod(root, 0o700)
    for current, dirs, files in os.walk(root):
        base = Path(current)
        for name in dirs:
            path = base / name
            if not path.is_symlink():
                os.chmod(path, 0o700)
        for name in files:
            path = base / name
            if not path.is_symlink():
                mode = stat.S_IMODE(path.lstat().st_mode)
                os.chmod(path, mode & 0o700)


def _copy_into_place(source: Path, target: Path) -> None:
    staging = target.with_name(f".migrating-{target.name}")
    if staging.is_dir() and not staging.is_symlink():
        shutil.rmtree(staging)
    elif staging.exists() or staging.is_symlink():
        staging.unlink()
    try:
        if source.is_dir():
            shutil.copytree(
                source,
                staging,
                symlinks=True,
                ignore=lambda directory, names: (
                    [n for n in names if n in _SINGLETONS] if Path(directory) == source else []
                ),
            )
        else:
            shutil.copy2(source, staging)
        _tighten(staging)
        os.replace(staging, target)
    except BaseException:
        if staging.is_dir() and not staging.is_symlink():
            shutil.rmtree(staging, ignore_errors=True)
        else:
            staging.unlink(missing_ok=True)
        raise


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _verify(destination: Path, source_manifests: dict[str, Manifest]) -> list[str]:
    """Problems with the copies at *destination*. Empty means verified."""
    problems: list[str] = []
    for name, expected in source_manifests.items():
        target = destination / name
        if _manifest(target) != expected:
            problems.append(f"{name} at {target} does not match the source")
            continue
        if name == COOKIE_FILENAME:
            try:
                parse_cookie_file(target)
            except Exception as exc:  # the type only: a message could quote a value
                problems.append(f"{target} is not a readable session ({type(exc).__name__})")
            if stat.S_IMODE(target.stat().st_mode) & 0o077:
                problems.append(f"{target} is readable by group or other")
        elif stat.S_IMODE(target.stat().st_mode) != 0o700:
            problems.append(f"{target} is not 0700")
    return problems


def migrate_data(
    source: Path | None = None,
    destination: Path | None = None,
    *,
    dry_run: bool = False,
    keep_source: bool = False,
    out: Callable[[str], None] = print,
) -> MigrationReport:
    """Move the old session into the plugin data directory. See the module docstring."""
    source = (source or legacy_state_dir()).expanduser()
    destination = (destination or state_dir()).expanduser()
    report = MigrationReport(source=source, destination=destination)

    def say(message: str) -> None:
        report.messages.append(message)
        out(message)

    try:
        manifests = _plan(report, say)
    except MigrationRefused as refusal:
        report.status = "refused"
        say(f"Refused: {refusal}. Nothing was changed.")
        return report

    pending = [e for e in report.entries if e.action in {"copy", "already_present"}]
    if not pending:
        report.status = "nothing_to_migrate"
        say(f"Nothing to migrate: no {COOKIE_FILENAME} or {BROWSER_PROFILE_DIRNAME}/ in {source}.")
        return report

    if dry_run:
        report.status = "dry_run"
        say("Dry run: nothing was changed.")
        return report

    copied: list[Path] = []
    try:
        ensure_private_dir(destination)
        for entry in pending:
            if entry.action == "copy":
                _copy_into_place(source / entry.name, destination / entry.name)
                copied.append(destination / entry.name)
            else:
                # Identical already, perhaps from an interrupted run: hold it to
                # the same permissions a fresh copy gets.
                _tighten(destination / entry.name)
    except OSError as exc:
        for path in copied:
            _remove(path)
        report.status = "refused"
        say(f"Refused: copying failed ({exc.strerror or type(exc).__name__}). "
            f"Copies from this run were removed; the source is untouched.")
        return report

    problems = _verify(destination, {e.name: manifests[e.name] for e in pending})
    if problems:
        for path in copied:
            _remove(path)
        report.status = "verification_failed"
        for problem in problems:
            say(f"Verification failed: {problem}")
        say("Copies from this run were removed; the source is untouched.")
        return report

    report.status = "migrated" if copied else "already_migrated"
    say(f"Verified {len(pending)} entr{'y' if len(pending) == 1 else 'ies'} at {destination}.")

    if keep_source:
        say(f"Kept the originals in {source} (--keep-source).")
    else:
        for entry in pending:
            _remove(source / entry.name)
        say(f"Removed the originals from {source}.")
    if report.left_in_place:
        say(f"Left in place in {source} (not part of this plugin's session): "
            f"{', '.join(report.left_in_place)}")
    return report


def _plan(report: MigrationReport, say: Callable[[str], None]) -> dict[str, Manifest]:
    """Inventory, validate and decide. Returns each present entry's source manifest."""
    source, destination = report.source, report.destination
    if source.resolve() == destination.resolve():
        raise MigrationRefused(f"source and destination are the same directory ({source})")

    say(f"Source:      {source}")
    say(f"Destination: {destination}")
    if not source.is_dir():
        report.entries = [EntryReport(COOKIE_FILENAME, "missing"),
                          EntryReport(BROWSER_PROFILE_DIRNAME, "missing")]
        return {}

    expected_kinds = {COOKIE_FILENAME: "file", BROWSER_PROFILE_DIRNAME: "dir"}
    report.left_in_place = sorted(
        child.name for child in source.iterdir() if child.name not in expected_kinds
    )

    manifests: dict[str, Manifest] = {}
    conflicts: list[str] = []
    for name, kind in expected_kinds.items():
        path = source / name
        if not path.exists() and not path.is_symlink():
            report.entries.append(EntryReport(name=name, kind="missing"))
            say(f"  {name}: not present")
            continue
        if path.is_symlink():
            raise MigrationRefused(f"{path} is a symlink; move the real {kind} yourself")
        if kind == "file" and not path.is_file() or kind == "dir" and not path.is_dir():
            raise MigrationRefused(f"{path} should be a {kind}")
        if kind == "dir" and _profile_in_use(path):
            raise MigrationRefused(
                f"{path} is in use by a running browser; stop the engine and any "
                f"'logos-login', then retry"
            )
        manifests[name] = _manifest(path)
        entry = _inventory(path, name, manifests[name])
        report.entries.append(entry)
        say(f"  {name}: {entry.files} file(s), {entry.bytes:,} bytes")

        if name == COOKIE_FILENAME:
            try:
                jar = parse_cookie_file(source / name)
            except Exception as exc:
                raise MigrationRefused(
                    f"{source / name} is not a readable session ({type(exc).__name__}); "
                    f"remove it or run 'logos-login' instead"
                ) from None
            say(f"    session with {len(jar.cookies)} cookie(s)")

        target = destination / name
        if target.exists() or target.is_symlink():
            if _manifest(target) == manifests[name]:
                entry.action = "already_present"
                say("    already present at the destination, identical")
            else:
                conflicts.append(str(target))
        else:
            entry.action = "copy"
            say("    will be copied")

    if conflicts:
        raise MigrationRefused(
            "the destination already holds a different "
            + " and ".join(conflicts)
            + "; move it aside or remove it if it is not wanted"
        )
    return manifests
