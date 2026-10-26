from gc_testkit.paths import RECIPES
from recommended_mcp.manifest import default_manifest_path, load_manifest


def test_manifest_carries_descriptions():
    root = RECIPES
    entries = load_manifest(default_manifest_path(root))
    by_name = {e.name: e.description for e in entries}
    assert by_name["computer"]  # непустое описание
    assert by_name["duckduckgo_search"]


def test_office_description_lists_powerpoint():

    from recommended_mcp.manifest import default_manifest_path, load_manifest

    root = RECIPES
    entries = {
        e.name: e.description
        for e in load_manifest(default_manifest_path(root))
    }
    assert "PowerPoint" in entries["office"]
