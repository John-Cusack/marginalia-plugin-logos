"""Discover the installed plugin the way core does, from outside any checkout.

Run with the interpreter of a clean environment that has the SDK and the plugin
wheel installed, from a directory that is not this repository:

    /tmp/venv/bin/python /path/to/scripts/check_installed.py

It proves, in order: the code comes from site-packages, not a checkout; core
is absent; the entry point and manifest are found through distribution
metadata without importing `logos`; every entry module, manifest resource and
migration is an installed file, which is what core 0.6's discovery checks; and
only then that every declared entry imports and matches.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import re
import sys
import sysconfig
from pathlib import Path


def main() -> int:
    problems: list[str] = []
    fail = problems.append

    here = Path.cwd().resolve()
    if (here / "logos" / "plugin.yaml").exists() or any(
        (Path(p) / "logos" / "plugin.yaml").exists() and "site-packages" not in p
        for p in sys.path if p
    ):
        fail("a checkout of logos is importable; run from outside the repository")
    if importlib.util.find_spec("research_engine") is not None:
        fail("core `research_engine` is installed; this smoke is SDK-only")

    from research_engine_sdk import entry_module, parse_manifest

    (entry_point,) = [
        ep for ep in importlib.metadata.entry_points(group="research_engine.plugins")
        if ep.name == "logos"
    ] or [None]
    if entry_point is None:
        fail("no research_engine.plugins entry point named logos")
        return _report(problems)
    if entry_point.value != "logos":
        fail(f"entry point value {entry_point.value!r} != 'logos'")

    dist = entry_point.dist
    files = {str(f): f for f in dist.files or []}
    if "logos/plugin.yaml" not in files:
        fail("logos/plugin.yaml is not a recorded file of the distribution")
        return _report(problems)
    manifest_path = Path(dist.locate_file(files["logos/plugin.yaml"])).resolve()
    purelib = Path(sysconfig.get_paths()["purelib"]).resolve()
    if purelib not in manifest_path.parents:
        fail(f"manifest resolved outside site-packages: {manifest_path}")

    manifest = parse_manifest(manifest_path)
    if "logos" in sys.modules:
        fail("discovery imported logos")
    for resource in manifest.resource_paths():
        if f"logos/{resource}" not in files:
            fail(f"manifest resource {resource} is not an installed file")
    for entry in manifest.entry_values():
        module = entry_module(entry)
        if module != "logos" and not module.startswith("logos."):
            fail(f"entry {entry} is outside the logos package")
        member = module.replace(".", "/")
        if f"{member}.py" not in files and f"{member}/__init__.py" not in files:
            fail(f"entry module {module} is not an installed file")
    migrations = sorted(
        int(match[1]) for name in files
        if (match := re.fullmatch(r"logos/db/migrations/(\d{3})_[a-z0-9_]+\.sql", name))
    )
    database = manifest.provides.database
    if database is None or migrations != list(range(1, database.current_revision + 1)):
        fail(f"installed migrations {migrations} do not reach the declared revision")

    # Load: only now import, as core does after approval.
    for entry in manifest.entry_values():
        module_name, attribute = entry.split(":")
        try:
            target = getattr(importlib.import_module(module_name), attribute)
        except Exception as exc:  # noqa: BLE001 — report every broken entry
            fail(f"entry {entry} failed to import: {exc!r}")
            continue
        origin = Path(importlib.import_module(module_name).__file__).resolve()
        if purelib not in origin.parents:
            fail(f"entry {entry} imported from outside site-packages: {origin}")
        tool = next((t for t in manifest.provides.mcp_tools if t.entry == entry), None)
        if tool and getattr(target, "_tool_id", None) != tool.id:
            fail(f"entry {entry} is not the handler for {tool.id}")
        if tool and getattr(target, "_tool_input_schema", None) != tool.input_schema:
            fail(f"{tool.id}: manifest input_schema differs from the handler's")

    revision = database.current_revision if database is not None else None
    print(f"{dist.metadata['Name']} {dist.version}: {len(manifest.provides.mcp_tools)} tools, "
          f"{len(manifest.entry_values())} entries, {len(manifest.resource_paths())} resources, "
          f"database revision {revision}")
    return _report(problems)


def _report(problems: list[str]) -> int:
    for problem in problems:
        print(f"FAIL {problem}")
    if not problems:
        print("OK installed plugin discovered and loaded from site-packages")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
