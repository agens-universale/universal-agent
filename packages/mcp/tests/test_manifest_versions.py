"""Версии source-MCP в манифесте — источник признака «есть обновление» (локально, без сети).

Манифест едет вместе с агентом: после обновления агента `install.version` рецепта — это
версия MCP в НОВОМ релизе, а `query_installed_version` даёт установленную. Их расхождение и
есть «есть что обновлять». Чтобы признак не врал, версия в манифесте обязана совпадать с
pyproject.toml соответствующего проекта в external/ — это и проверяем.
"""

import tomllib
from pathlib import Path

from gc_testkit.paths import RECIPES, REPO
from recommended_mcp.install import _vendor_candidates
from recommended_mcp.manifest import load_manifest

_ROOT = Path(__file__).resolve().parents[2]


def _pyproject_version(source_dir: Path) -> str:
    data = tomllib.loads(
        (source_dir / "pyproject.toml").read_text(encoding="utf-8")
    )
    return data["project"]["version"]


def test_source_mcp_versions_match_pyproject():
    manifest_path = RECIPES / "config.yml"
    root = manifest_path.parent
    source_entries = [
        e for e in load_manifest(manifest_path) if e.install.type == "source"
    ]
    assert source_entries, "в манифесте нет source-MCP — тест потерял смысл"
    for entry in source_entries:
        # install.source — ИМЯ каталога, а не путь (с 18.08.2026): исходники
        # ищет тот же резолвер, что и установщик.
        src_dir = next(
            c
            for c in _vendor_candidates(str(entry.install.source), root)
            if (c / "pyproject.toml").exists()
        )
        assert entry.install.version, (
            f"{entry.name}: install.version не задан в манифесте"
        )
        assert entry.install.version == _pyproject_version(src_dir), (
            f"{entry.name}: версия в манифесте разошлась с {src_dir}/pyproject.toml"
        )


def test_available_update_for_source_uses_manifest_version():
    """source-MCP: установлена 0.2.0, в манифесте (новый релиз) 0.3.0 → есть обновление."""
    from recommended_mcp.manifest import InstallSpec, MCPEntry
    from recommended_mcp.runtime.cli import _available_update

    entry = MCPEntry(
        name="office",
        description="",
        path=Path("./office"),
        install=InstallSpec(
            type="source",
            source=Path("../external/office-mcp"),
            dist="office-mcp",
            version="0.3.0",
        ),
    )
    assert _available_update(entry, "0.2.0") == "0.3.0"
    assert (
        _available_update(entry, "0.3.0") is None
    )  # та же версия — обновлять нечего
    assert _available_update(entry, None) is None  # версию не узнали — не врём


def test_available_update_none_without_manifest_version():
    """Рецепт без version (напр. npx) — признака нет, а не ложное «обновление»."""
    from recommended_mcp.manifest import InstallSpec, MCPEntry
    from recommended_mcp.runtime.cli import _available_update

    entry = MCPEntry(
        name="browser_playwright",
        description="",
        path=Path("./b"),
        install=InstallSpec(type="npx", npm_package="@playwright/mcp"),
    )
    assert _available_update(entry, "1.0.0") is None


def test_playwright_browser_comes_from_the_wired_package():
    """Браузер ставит playwright того же `@playwright/mcp`, что запускается:
    отдельный `playwright` тянет последнюю версию — чужая ревизия Chromium
    и свои требования к Node. Запуск — на этом Chromium, не на системном
    Chrome."""
    import yaml

    entry = next(
        e
        for e in load_manifest(RECIPES / "config.yml")
        if e.name == "browser_playwright"
    )
    pinned = next(
        arg
        for cmd in entry.install.commands
        for arg in cmd
        if arg.startswith("@playwright/mcp@")
    )
    assert (
        "npx",
        "-y",
        pinned,
        "install-browser",
        "--with-deps",
        "chromium",
    ) in entry.install.commands
    assert all("playwright" not in cmd for cmd in entry.install.commands)
    snippet = yaml.safe_load(
        (
            RECIPES / "browser_playwright" / "agent_config.snippet.yml"
        ).read_text(encoding="utf-8")
    )
    args = snippet["browser_playwright"]["args"]
    assert pinned in args
    assert args[args.index("--browser") + 1] == "chromium"


def test_demo_config_playwright_example_matches_snippet():
    """Закомментированный пример в demo_config.yml копируют руками — его
    аргументы обязаны совпадать со сниппетом (кэш там — `<cache>`)."""
    import yaml

    snippet = yaml.safe_load(
        (
            RECIPES / "browser_playwright" / "agent_config.snippet.yml"
        ).read_text(encoding="utf-8")
    )
    expected = [
        a.replace("{cache}", "<cache>")
        for a in snippet["browser_playwright"]["args"]
    ]
    demo = (REPO / "demo_config.yml").read_text(encoding="utf-8")
    assert "# browser_playwright" in demo, "пример пропал из demo_config.yml"
    block = demo.split("# browser_playwright", 1)[1].split("\n  meta:", 1)[0]
    args_line = next(
        (line for line in block.splitlines() if "args:" in line), None
    )
    assert args_line, "в примере browser_playwright нет строки args"
    assert yaml.safe_load(args_line.split("args:", 1)[1]) == expected
