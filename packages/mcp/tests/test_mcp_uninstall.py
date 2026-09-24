"""Деинсталл рецептов: определение имени дистрибутива для `pip uninstall`.

Раскладки для type=source здесь НАСТОЯЩИЕ, а не синтетические. До 28.08.2026
тесты собирали ``tmp_path/office-mcp`` — папку MCP прямо в каталоге рецептов.
Такой раскладки не существует ни в колесе (там рецепты и ``external/vendor`` —
соседи), ни в рабочей копии (там ``<repo>/packages/mcp/external/vendor``),
поэтому они
проходили на арифметике ``root / install.source``, которая в жизни не попадала
в цель ни разу: ``_dist_name`` всегда возвращал None, и деинсталл рецепта без
явного ``dist:`` молча пропускался. Теперь ``_dist_name`` ищет исходники тем же
``_vendor_candidates``, что и установщик, и тесты собирают то, что он ищет.
"""

from pathlib import Path

from recommended_mcp import install as inst
from recommended_mcp.manifest import InstallSpec, MCPEntry


def _wheel_layout(tmp_path: Path, name: str) -> Path:
    """Раскладка колеса: site-packages/recommended_mcp/external/vendor/.

    Возвращает root — каталог рецептов, который получает _dist_name.
    """
    recipes = tmp_path / "recommended_mcp"
    recipes.mkdir(exist_ok=True)
    src = recipes / "external" / "vendor" / name
    src.mkdir(parents=True)
    (src / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "1.0"\n', encoding="utf-8"
    )
    return recipes


def test_dist_name_pypi():
    e = MCPEntry(
        name="ddg",
        path=Path("."),
        install=InstallSpec(type="pypi", package="ddgs-mcp==0.2.0"),
    )
    assert inst._dist_name(e) == "ddgs-mcp"


def test_dist_name_source_reads_pyproject(tmp_path):
    root = _wheel_layout(tmp_path, "office-mcp")
    e = MCPEntry(
        name="office",
        path=Path("."),
        install=InstallSpec(type="source", source=Path("office-mcp")),
    )
    assert inst._dist_name(e, root=root) == "office-mcp"


def test_uninstall_entry_pypi_calls_pip(monkeypatch):
    calls = []
    monkeypatch.setattr(
        inst.subprocess, "run", lambda argv, **kw: calls.append(argv)
    )
    monkeypatch.setattr(inst, "pip_argv", lambda *a: ["PY", "-m", "pip", *a])
    e = MCPEntry(
        name="ddg",
        path=Path("."),
        install=InstallSpec(type="pypi", package="ddgs-mcp==0.2.0"),
    )
    inst.uninstall_entry(e, Path("."))
    assert calls and "uninstall" in calls[0] and "ddgs-mcp" in calls[0]


def test_uninstall_entry_npx_calls_npm(monkeypatch):
    calls = []
    monkeypatch.setattr(
        inst.subprocess, "run", lambda argv, **kw: calls.append(argv)
    )
    monkeypatch.setattr(
        inst, "_node_install_paths", lambda dep: {"npm": "NPM"}
    )
    monkeypatch.setattr(
        "gc_core.config_modules.env_vars.gc_paths",
        lambda: {
            "data": Path("."),
            "config": Path("."),
            "cache": Path("."),
            "state": Path("."),
        },
    )
    e = MCPEntry(
        name="pw",
        path=Path("."),
        install=InstallSpec(type="npx", npm_package="@playwright/mcp"),
    )
    inst.uninstall_entry(e, Path("."))
    assert calls and "uninstall" in calls[0] and "@playwright/mcp" in calls[0]


def test_dist_name_wheel(tmp_path):
    dist = tmp_path / "rec" / "dist"
    dist.mkdir(parents=True)
    (dist / "office-mcp-1.0-py3-none-any.whl").write_text("", encoding="utf-8")
    e = MCPEntry(
        name="office", path=Path("rec"), install=InstallSpec(type="wheel")
    )
    assert inst._dist_name(e, root=tmp_path) == "office-mcp"


def test_dist_name_prefers_explicit_dist(tmp_path):
    # source-папки на диске НЕТ — _dist_name всё равно отдаёт install.dist
    e = MCPEntry(
        name="office",
        path=Path("."),
        install=InstallSpec(
            type="source",
            source=Path("../external/office-mcp"),
            dist="office-mcp",
        ),
    )
    assert inst._dist_name(e, root=tmp_path) == "office-mcp"


def test_dist_name_fallback_pyproject_when_no_dist(tmp_path):
    root = _wheel_layout(tmp_path, "science-mcp")
    e = MCPEntry(
        name="science",
        path=Path("."),
        install=InstallSpec(type="source", source=Path("science-mcp")),
    )
    assert inst._dist_name(e, root=root) == "science-mcp"


def test_dist_name_source_finds_pyproject_in_repo_layout(tmp_path):
    """Раскладка рабочей копии: исходники в <repo>/packages/mcp/external/vendor.

    Второй кандидат _vendor_candidates находится только по СТРОГОМУ маркеру —
    packages/core/pyproject.toml с name = "gc-core".
    """
    (tmp_path / "packages" / "core").mkdir(parents=True)
    (tmp_path / "packages" / "core" / "pyproject.toml").write_text(
        '[project]\nname = "gc-core"\n', encoding="utf-8"
    )
    src = tmp_path / "packages" / "mcp" / "external" / "vendor" / "office-mcp"
    src.mkdir(parents=True)
    (src / "pyproject.toml").write_text(
        '[project]\nname = "office-mcp"\n', encoding="utf-8"
    )
    root = tmp_path / "packages" / "mcp" / "src" / "recommended_mcp"
    root.mkdir(parents=True)

    e = MCPEntry(
        name="office",
        path=Path("."),
        install=InstallSpec(type="source", source=Path("office-mcp")),
    )
    assert inst._dist_name(e, root=root) == "office-mcp"


def test_dist_name_source_legacy_format(tmp_path, monkeypatch):
    """Дореформенный путь в install.source не мешает найти имя дистрибутива."""
    monkeypatch.setattr(inst, "_LEGACY_SOURCE_WARNED", set())
    root = _wheel_layout(tmp_path, "office-mcp")
    e = MCPEntry(
        name="office",
        path=Path("."),
        install=InstallSpec(
            type="source", source=Path("../external/office-mcp")
        ),
    )
    assert inst._dist_name(e, root=root) == "office-mcp"


def test_dist_name_skips_broken_pyproject_and_keeps_looking(tmp_path):
    """Битый pyproject у первого кандидата не обрывает поиск.

    Ровно тот класс промаха, который правка чинит: раньше на любой осечке
    разбора функция сдавалась, вместо того чтобы дойти до вендоров
    рабочей копии.
    """
    (tmp_path / "packages" / "core").mkdir(parents=True)
    (tmp_path / "packages" / "core" / "pyproject.toml").write_text(
        '[project]\nname = "gc-core"\n', encoding="utf-8"
    )
    root = tmp_path / "packages" / "mcp" / "src" / "recommended_mcp"
    root.mkdir(parents=True)
    # Первый кандидат (раскладка колеса, root/external/vendor) — битый.
    broken = root / "external" / "vendor" / "office-mcp"
    broken.mkdir(parents=True)
    (broken / "pyproject.toml").write_text(
        "[project\nname = ", encoding="utf-8"
    )
    # Второй (рабочая копия) — целый.
    good = tmp_path / "packages" / "mcp" / "external" / "vendor" / "office-mcp"
    good.mkdir(parents=True)
    (good / "pyproject.toml").write_text(
        '[project]\nname = "office-mcp"\n', encoding="utf-8"
    )

    e = MCPEntry(
        name="office",
        path=Path("."),
        install=InstallSpec(type="source", source=Path("office-mcp")),
    )
    assert inst._dist_name(e, root=root) == "office-mcp"
