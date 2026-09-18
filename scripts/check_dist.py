"""Inspect built artifacts, not the checkout: what users install is what is checked.

    python scripts/check_dist.py dist/

Fails (exit 1) listing every problem found in the wheel and sdist.
Needs only the standard library and PyYAML.
"""

from __future__ import annotations

import email.parser
import re
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

import yaml

NAME = "marginalia-ai-plugin-logos"
PACKAGE = "logos"

#: Nothing matching these may ship. Cookies, browser profiles, checkpoints and
#: local configuration are the operator's data; tests, caches and the vendored
#: pre-release SDK are development scaffolding.
FORBIDDEN = [
    re.compile(p)
    for p in (
        r"(^|/)cookies\.json$",
        r"(^|/)browser-profile(/|$)",
        r"(^|/)\.env($|\.)",
        r"(^|/)__pycache__(/|$)",
        r"\.py[co]$",
        r"(^|/)\.pytest_cache(/|$)",
        r"(^|/)tests?/",
        r"(^|/)vendor(/|$)",
        r"(^|/)research_engine_sdk(/|$)",
        r"(^|/)checkpoints?(/|$)",
        r"\.(sqlite3?|db|dump)$",
        r"(^|/)\.logos-mcp(/|$)",
        r"(^|/)uv\.lock$",
    )
]


def _forbidden(names: list[str], label: str) -> list[str]:
    return [
        f"{label}: forbidden file {name}"
        for name in names
        for pattern in FORBIDDEN
        if pattern.search(name)
    ]


#: Where the numbered SQL migrations live inside the package. Core 0.6's manifest
#: names only the status/upgrade entries, not the files they apply.
MIGRATIONS_DIR = "db/migrations"


def _manifest_resources(manifest_bytes: bytes) -> list[str]:
    manifest = yaml.safe_load(manifest_bytes)
    provides = manifest.get("provides", {})
    found = [s["file"] for s in provides.get("extraction_schemas", [])]
    found += [v["file"] for v in provides.get("vocabularies", [])]
    if provides.get("database"):
        found.append(MIGRATIONS_DIR)
    return found


def _check_resources(names: set[str], root: str, manifest_bytes: bytes, label: str) -> list[str]:
    problems = []
    for resource in _manifest_resources(manifest_bytes):
        path = f"{root}/{resource}"
        if path not in names and not any(n.startswith(path + "/") for n in names):
            problems.append(f"{label}: manifest resource {resource} missing")
    migrations = yaml.safe_load(manifest_bytes)["provides"].get("database")
    if migrations:
        prefix = f"{root}/{MIGRATIONS_DIR}/"
        sql = sorted(n for n in names if n.startswith(prefix) and n.endswith(".sql"))
        revisions = [int(PurePosixPath(n).name.split("_", 1)[0]) for n in sql]
        if revisions != list(range(1, len(sql) + 1)):
            problems.append(f"{label}: migrations not contiguous from 1: {revisions}")
        if revisions and revisions[-1] != migrations["current_revision"]:
            problems.append(
                f"{label}: current_revision {migrations['current_revision']} "
                f"!= newest migration {revisions[-1]}"
            )
    return problems


def check_wheel(path: Path, version: str) -> list[str]:
    problems: list[str] = []
    with zipfile.ZipFile(path) as wheel:
        names = set(wheel.namelist())
        dist_info = f"{NAME.replace('-', '_')}-{version}.dist-info"

        problems += _forbidden(sorted(names), "wheel")
        top_level = {n.split("/", 1)[0] for n in names} - {dist_info}
        if top_level != {PACKAGE}:
            problems.append(f"wheel: unexpected top-level entries {sorted(top_level)}")

        manifest_name = f"{PACKAGE}/plugin.yaml"
        if manifest_name not in names:
            return [*problems, "wheel: logos/plugin.yaml missing"]
        manifest_bytes = wheel.read(manifest_name)
        problems += _check_resources(names, PACKAGE, manifest_bytes, "wheel")
        manifest = yaml.safe_load(manifest_bytes)
        for tool in manifest["provides"]["mcp_tools"]:
            module = tool["entry"].split(":")[0].replace(".", "/") + ".py"
            if module not in names:
                problems.append(f"wheel: tool module {module} missing")

        entry_points = wheel.read(f"{dist_info}/entry_points.txt").decode()
        if not re.search(r"^\[research_engine\.plugins\]\s*\nlogos = logos\s*$",
                         entry_points, re.M):
            problems.append("wheel: entry point research_engine.plugins logos = logos missing")
        for script in ("logos-login = logos.cli.login:main",
                       "logos-diagnose = logos.cli.diagnose:main"):
            if script not in entry_points:
                problems.append(f"wheel: console script {script!r} missing")

        if f"{dist_info}/licenses/LICENSE" not in names:
            problems.append("wheel: LICENSE not in dist-info/licenses")
        metadata = email.parser.Parser().parsestr(wheel.read(f"{dist_info}/METADATA").decode())
        problems += _check_metadata(metadata, version, "wheel")
    return problems


def _check_metadata(metadata, version: str, label: str) -> list[str]:
    problems = []
    expect = {
        "Name": NAME,
        "Version": version,
        "License-Expression": "Apache-2.0",
        "Requires-Python": ">=3.11",
        "Description-Content-Type": "text/markdown",
    }
    for field, value in expect.items():
        if metadata.get(field) != value:
            problems.append(f"{label}: {field} is {metadata.get(field)!r}, expected {value!r}")
    if "LICENSE" not in (metadata.get_all("License-File") or []):
        problems.append(f"{label}: License-File LICENSE missing")
    if any(c.startswith("License ::") for c in metadata.get_all("Classifier") or []):
        problems.append(f"{label}: license classifier conflicts with License-Expression")
    requires = metadata.get_all("Requires-Dist") or []
    if not any(re.fullmatch(r"marginalia-ai-sdk\s*(<0\.7,\s*>=0\.6|>=0\.6,\s*<0\.7)", r)
               for r in requires):
        problems.append(f"{label}: Requires-Dist lacks marginalia-ai-sdk>=0.6,<0.7: {requires}")
    # The plugin depends on the SDK, never on the core application, and never on
    # a pre-rename MarginaliaAI distribution.
    for forbidden in ("marginalia-ai", "research-engine", "research-engine-sdk",
                      "research-engine-plugin-logos", "marginalia-plugin-logos"):
        if any(r.split(";")[0].strip().split()[0] == forbidden for r in requires):
            problems.append(f"{label}: must not depend on {forbidden}")
    if not any(re.search(r"^playwright\b.*;\s*extra\s*==\s*['\"]auth['\"]", r) for r in requires):
        problems.append(f"{label}: playwright is not confined to the auth extra")
    if any(r.startswith("playwright") and "extra" not in r for r in requires):
        problems.append(f"{label}: playwright is not confined to the auth extra")
    urls = {u.split(",", 1)[0].strip() for u in metadata.get_all("Project-URL") or []}
    for key in ("Homepage", "Source", "Issues", "Changelog"):
        if key not in urls:
            problems.append(f"{label}: Project-URL {key} missing")
    if len(metadata.get_payload() or "") < 500:
        problems.append(f"{label}: README long description missing or trivial")
    return problems


def check_sdist(path: Path, version: str) -> list[str]:
    problems: list[str] = []
    root = f"{NAME.replace('-', '_')}-{version}"
    with tarfile.open(path) as sdist:
        names = {m.name for m in sdist.getmembers() if m.isfile()}
        relative = sorted(n.removeprefix(root + "/") for n in names)
        problems += _forbidden(relative, "sdist")
        # Hatchling always adds the VCS ignore file so a build from the sdist
        # honours it; it carries no data.
        allowed_top = {PACKAGE, "README.md", "CHANGELOG.md", "LICENSE", "pyproject.toml",
                       "PKG-INFO", ".gitignore"}
        extra = {r.split("/", 1)[0] for r in relative} - allowed_top
        if extra:
            problems.append(f"sdist: unexpected top-level entries {sorted(extra)}")
        manifest_name = f"{root}/{PACKAGE}/plugin.yaml"
        if manifest_name not in names:
            return [*problems, "sdist: logos/plugin.yaml missing"]
        manifest_bytes = sdist.extractfile(manifest_name).read()
        problems += _check_resources(names, f"{root}/{PACKAGE}", manifest_bytes, "sdist")
        for required in ("README.md", "CHANGELOG.md", "LICENSE", "pyproject.toml"):
            if f"{root}/{required}" not in names:
                problems.append(f"sdist: {required} missing")
        pkg_info = email.parser.Parser().parsestr(
            sdist.extractfile(f"{root}/PKG-INFO").read().decode()
        )
        problems += _check_metadata(pkg_info, version, "sdist")
    return problems


def main(argv: list[str]) -> int:
    dist = Path(argv[1] if len(argv) > 1 else "dist")
    wheels = sorted(dist.glob("marginalia_ai_plugin_logos-*.whl"))
    sdists = sorted(dist.glob("marginalia_ai_plugin_logos-*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        print(f"expected exactly one wheel and one sdist in {dist}, found {wheels + sdists}")
        return 1
    version = wheels[0].name.split("-")[1]
    if sdists[0].name != f"marginalia_ai_plugin_logos-{version}.tar.gz":
        print(f"wheel and sdist versions disagree: {wheels[0].name}, {sdists[0].name}")
        return 1

    problems = check_wheel(wheels[0], version) + check_sdist(sdists[0], version)
    for problem in problems:
        print(f"FAIL {problem}")
    if not problems:
        print(f"OK {wheels[0].name} and {sdists[0].name}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
