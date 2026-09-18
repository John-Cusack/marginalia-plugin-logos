"""The plugin as core discovers it: entry point, static manifest, contributions.

Core reads `logos/plugin.yaml` without importing `logos`, approves what it says,
then imports every entry. These tests hold the manifest to the code on both
sides of that, using only the SDK: a tool the manifest omits is never
registered, and an entry that does not resolve fails the whole plugin's load.
"""

from __future__ import annotations

import ast
import importlib
import importlib.metadata
import inspect
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from research_engine_sdk import entry_module, parse_manifest

import logos

pytestmark = pytest.mark.unit

PACKAGE_DIR = Path(logos.__file__).resolve().parent
MANIFEST = parse_manifest(PACKAGE_DIR / "plugin.yaml")


def _resolve(entry: str):
    module, attribute = entry.split(":")
    return getattr(importlib.import_module(module), attribute)


def test_the_manifest_is_schema_v2_and_complete_against_the_package() -> None:
    """What core 0.6's discovery rejects a distribution for, short of importing it:
    an entry outside the top-level package or with no module file, and a declared
    resource that is not there."""
    assert MANIFEST.schema_version == 2
    assert MANIFEST.plugin_id == "logos"
    for entry in MANIFEST.entry_values():
        module = entry_module(entry)
        assert module == "logos" or module.startswith("logos."), entry
        relative = PACKAGE_DIR.parent / module.replace(".", "/")
        assert relative.with_suffix(".py").is_file() or (relative / "__init__.py").is_file(), (
            f"entry module {module!r} is absent"
        )
    for resource in MANIFEST.resource_paths():
        assert (PACKAGE_DIR / resource).is_file(), f"declared resource {resource!r} is absent"


def test_legacy_manifest_fields_are_gone() -> None:
    raw = yaml.safe_load((PACKAGE_DIR / "plugin.yaml").read_text())
    for field in ("name", "version", "author", "description", "license"):
        assert field not in raw
    assert "pip" not in raw["requires"]
    assert not (PACKAGE_DIR.parent / "pack.yaml").exists(), "one manifest, not two"


@pytest.mark.parametrize("tool", MANIFEST.provides.mcp_tools, ids=lambda t: t.id)
def test_every_declared_tool_resolves_to_its_own_handler(tool) -> None:
    handler = _resolve(tool.entry)
    assert handler._tool_id == tool.id
    # Core registers the manifest's schema, and validates arguments against it
    # before the handler runs; the decorator's is the one the handler was
    # written to. They must be one schema.
    assert tool.input_schema == handler._tool_input_schema
    assert inspect.iscoroutinefunction(handler)
    kinds = {p.kind for p in inspect.signature(handler).parameters.values()}
    assert inspect.Parameter.VAR_KEYWORD in kinds, "core passes clients and context by keyword"


def test_no_tool_handler_is_left_out_of_the_manifest() -> None:
    declared = {tool.id for tool in MANIFEST.provides.mcp_tools}
    found = set()
    for path in sorted((PACKAGE_DIR / "tools").glob("*.py")):
        module = importlib.import_module(f"logos.tools.{path.stem}")
        handler = getattr(module, "handler", None)
        if handler is not None and hasattr(handler, "_tool_id"):
            found.add(handler._tool_id)
    assert found == declared


def test_the_chunker_and_document_type_agree() -> None:
    (chunker,) = MANIFEST.provides.chunkers
    cls = _resolve(chunker.entry)
    assert cls.id == chunker.id
    assert {d.default_chunker for d in MANIFEST.provides.document_types} == {chunker.id}


def test_the_filter_extension_has_the_shape_core_registers() -> None:
    """The protocol lives in core (`research_engine.domain.filter_extension`),
    not the SDK, so its members are checked by name here and `isinstance` is
    checked against core's own protocol in the integration suite."""
    (contribution,) = MANIFEST.provides.filter_extensions
    extension = _resolve(contribution.entry)()
    assert extension.filter_id == contribution.id
    assert extension.input_schema["type"] == "object"
    assert extension.description
    assert callable(extension.build_clause)


def test_extraction_schemas_are_package_resources_that_parse() -> None:
    for schema in MANIFEST.provides.extraction_schemas:
        body = yaml.safe_load((PACKAGE_DIR / schema.file).read_text())
        assert body, schema.file


def test_the_database_contribution_matches_the_migrations_shipped() -> None:
    database = MANIFEST.provides.database
    assert database is not None
    files = sorted((PACKAGE_DIR / "db" / "migrations").glob("*.sql"))
    revisions = [int(re.match(r"(\d+)_", f.name).group(1)) for f in files]
    assert revisions == list(range(1, len(files) + 1))
    assert database.current_revision == revisions[-1]
    for entry in (database.status_entry, database.upgrade_entry):
        assert inspect.iscoroutinefunction(_resolve(entry))


def test_network_is_an_allowlist_not_full_access() -> None:
    assert MANIFEST.permissions.network == "egress"
    assert MANIFEST.permissions.network_allowlist
    for host in MANIFEST.permissions.network_allowlist:
        assert host == "logos.com" or host.endswith((".logos.com", ".faithlife.com")), host


def test_the_distribution_advertises_the_plugin_entry_point() -> None:
    (entry_point,) = [
        ep for ep in importlib.metadata.entry_points(group="research_engine.plugins")
        if ep.name == "logos"
    ]
    assert entry_point.value == "logos"
    assert entry_point.dist.name == "marginalia-ai-plugin-logos"


def test_the_manifest_can_be_read_without_importing_the_plugin() -> None:
    """What core's discovery does, in a clean interpreter."""
    script = (
        "import importlib.util, sys\n"
        "from research_engine_sdk import parse_manifest\n"
        "spec = importlib.util.find_spec('logos')\n"
        "from pathlib import Path\n"
        "root = Path(next(iter(spec.submodule_search_locations)))\n"
        "manifest = parse_manifest(root / 'plugin.yaml')\n"
        "assert manifest.plugin_id == 'logos'\n"
        "assert 'logos' not in sys.modules, 'discovery imported the plugin'\n"
        "print(len(manifest.provides.mcp_tools))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == str(len(MANIFEST.provides.mcp_tools))


def test_no_runtime_module_imports_the_engine() -> None:
    """Published plugins depend on the SDK only (plugin -> SDK <- core)."""
    offenders = []
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names = [node.module]
            offenders += [
                f"{path.relative_to(PACKAGE_DIR.parent)}:{node.lineno} {name}"
                for name in names
                if name.split(".")[0] == "research_engine"
            ]
    assert offenders == []
