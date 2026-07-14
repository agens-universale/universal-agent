"""Версии source-MCP в манифесте — источник признака «есть обновление» (локально, без сети).

Манифест едет вместе с агентом: после обновления агента `install.version` рецепта — это
версия MCP в НОВОМ релизе, а `query_installed_version` даёт установленную. Их расхождение и
есть «есть что обновлять». Чтобы признак не врал, версия в манифесте обязана совпадать с
pyproject.toml соответствующего проекта в external/ — это и проверяем.
"""
import tomllib
from pathlib import Path

from recommended_mcp.manifest import load_manifest

_ROOT = Path(__file__).resolve().parents[2]


def _pyproject_version(source_dir: Path) -> str:
    data = tomllib.loads((source_dir / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def test_source_mcp_versions_match_pyproject():
    manifest_path = _ROOT / "recommended_mcp" / "config.yml"
    root = manifest_path.parent
    source_entries = [e for e in load_manifest(manifest_path) if e.install.type == "source"]
    assert source_entries, "в манифесте нет source-MCP — тест потерял смысл"
    for entry in source_entries:
        src_dir = (root / entry.install.source).resolve()
        assert entry.install.version, f"{entry.name}: install.version не задан в манифесте"
        assert entry.install.version == _pyproject_version(src_dir), (
            f"{entry.name}: версия в манифесте разошлась с {src_dir}/pyproject.toml"
        )


def test_available_update_for_source_uses_manifest_version():
    """source-MCP: установлена 0.2.0, в манифесте (новый релиз) 0.3.0 → есть обновление."""
    from mcp_runtime.cli import _available_update
    from recommended_mcp.manifest import InstallSpec, MCPEntry
    entry = MCPEntry(name="office", description="", path=Path("./office"),
                     install=InstallSpec(type="source", source=Path("../external/office-mcp"),
                                         dist="office-mcp", version="0.3.0"))
    assert _available_update(entry, "0.2.0") == "0.3.0"
    assert _available_update(entry, "0.3.0") is None          # та же версия — обновлять нечего
    assert _available_update(entry, None) is None             # версию не узнали — не врём


def test_available_update_none_without_manifest_version():
    """Рецепт без version (напр. npx) — признака нет, а не ложное «обновление»."""
    from mcp_runtime.cli import _available_update
    from recommended_mcp.manifest import InstallSpec, MCPEntry
    entry = MCPEntry(name="browser_playwright", description="", path=Path("./b"),
                     install=InstallSpec(type="npx", npm_package="@playwright/mcp"))
    assert _available_update(entry, "1.0.0") is None
