from pathlib import Path
from recommended_mcp.manifest import load_manifest, resolve_manifest_path
from gc_testkit.paths import RECIPES


def test_manifest_carries_descriptions():
    root = RECIPES
    entries = load_manifest(resolve_manifest_path(root, None))
    by_name = {e.name: e.description for e in entries}
    assert by_name["computer"]                       # непустое описание
    assert by_name["duckduckgo_search"]


def test_office_description_lists_powerpoint():
    from pathlib import Path
    from recommended_mcp.manifest import load_manifest, resolve_manifest_path
    root = RECIPES
    entries = {e.name: e.description for e in load_manifest(resolve_manifest_path(root, None))}
    assert "PowerPoint" in entries["office"]
