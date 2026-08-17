"""Контракт упаковки: три непересекающихся дистрибутива из одного src/.

До 0.4.0 колесо было одно, и тест проверял, что в нём лежит ВСЁ, включая
tui/styles.tcss. Теперь tui и server уехали в отдельные дистрибутивы,
собираемые из того же дерева, и главный инвариант сменился: три колеса не
должны заявлять права ни на один общий путь — иначе установка двух профилей
перетирает файлы, и чья версия победит, решает порядок распаковки.
"""
import itertools
import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# Ассеты живут в ЯДРЕ: bundled_assets.bundled_root() резолвится от своего
# __file__, то есть от каталога ядра. Промпт TUI тоже здесь — иначе фронтенд,
# находясь в другом дистрибутиве, его бы не нашёл.
CORE_REQUIRED = [
    "prompts/chat_completions_system_prompt.md",
    "prompts/chat_completions_tui_special_instructions.md",
    "recommended_mcp/config.yml",
    "config.default.yml",
    "gc_core/config.py",
    "gc_core/tui_entry.py",     # шим точки входа gc-ua обязан быть в ядре
    "gc_core/serve_entry.py",
]

# Ядро не должно содержать код фронтендов — ради этого всё и затевалось.
CORE_FORBIDDEN = ["gc_tui/", "gc_cli/", "gc_server/"]

MANIFESTS = {"gc_ua": ".", "gc_core": "packages/core", "gc_cli": "packages/cli",
             "gc_tui": "packages/tui", "gc_server": "packages/server"}


def _build_all(out: Path) -> dict:
    for sub in MANIFESTS.values():
        r = subprocess.run(
            ["uv", "build", "--wheel", "-o", str(out), sub],
            cwd=str(REPO), capture_output=True, text=True,
        )
        assert r.returncode == 0, r.stderr
    wheels = {}
    for w in out.glob("*.whl"):
        key = w.name.split("-")[0]
        names = set(zipfile.ZipFile(w).namelist())
        wheels[key] = {n for n in names if not n.split("/")[0].endswith(".dist-info")}
    return wheels


@pytest.fixture(scope="module")
def wheels(tmp_path_factory):
    if shutil.which("uv") is None:
        pytest.skip("uv not on PATH")
    return _build_all(tmp_path_factory.mktemp("dist"))


def test_all_distributions_built(wheels):
    assert set(wheels) == set(MANIFESTS), sorted(wheels)


def test_wheels_are_pairwise_disjoint(wheels):
    """Главный инвариант разделения: ни одного общего пути между колёсами."""
    for a, b in itertools.combinations(sorted(wheels), 2):
        overlap = wheels[a] & wheels[b]
        assert not overlap, f"{a} и {b} заявляют общие пути: {sorted(overlap)[:10]}"


def test_core_wheel_bundles_assets(wheels):
    names = wheels["gc_core"]
    for needed in CORE_REQUIRED:
        assert needed in names, f"{needed} нет в колесе ядра; образец {sorted(names)[:10]}"
    assert any(n.startswith("skills/") for n in names), "skills/ отсутствует"
    assert any(n.startswith("recommended_mcp/") and n.endswith(".py") for n in names)
    assert "external/ddgs-mcp/pyproject.toml" in names, "external/ddgs-mcp не поставляется"
    assert "external/ddgs-mcp/src/ddgs_mcp/server.py" in names


def test_testkit_never_ships(wheels):
    """Тест-кит — инструмент разработки, в поставку он попадать не должен.

    gc_testkit лежит в packages/ рядом с настоящими пакетами и подхватывается
    workspace-ом, поэтому единственная защита от его утечки — проверка колёс.
    """
    for dist, names in wheels.items():
        leaked = [n for n in names if n.split("/")[0] == "gc_testkit"]
        assert not leaked, f"{dist} тащит тестовую инфраструктуру: {leaked[:5]}"


def test_core_wheel_excludes_frontends(wheels):
    """Ради этого всё и затевалось: headless-установка не должна класть на диск
    код фронтендов."""
    names = wheels["gc_core"]
    for bad in CORE_FORBIDDEN:
        hit = [n for n in names if n == bad or n.startswith(bad)]
        assert not hit, f"{bad} просочился в колесо ядра: {hit[:5]}"


def test_tui_wheel_contents(wheels):
    names = wheels["gc_tui"]
    assert {n.split("/")[0] for n in names} == {"gc_tui"}, sorted(names)[:10]
    assert "gc_tui/styles.tcss" in names, "стили TUI обязаны ехать вместе с пакетом"
    assert "gc_tui/diff_tui/tracking.py" in names, "diff_tui — часть TUI-профиля"


def test_server_wheel_contents(wheels):
    names = wheels["gc_server"]
    assert {n.split("/")[0] for n in names} == {"gc_server"}, sorted(names)[:10]
    for m in ("gc_server/server.py", "gc_server/rest_api/__init__.py",
              "gc_server/swarm/runner.py"):
        assert m in names, m


def test_cli_wheel_contents(wheels):
    names = wheels["gc_cli"]
    assert {n.split("/")[0] for n in names} == {"gc_cli"}, sorted(names)[:10]
    assert "gc_cli/cli.py" in names


def test_every_profile_declares_all_commands():
    """`uv tool install` экспортирует в PATH скрипты ТОЛЬКО запрошенного
    дистрибутива. Поэтому команды объявляет каждый профиль — иначе установка
    листа дала бы окружение вообще без команд (проверено живьём)."""
    expected = {"gc-cli", "gc-ua", "gc-serve", "gc-mcp", "gc-mcp-install"}
    for name, sub in MANIFESTS.items():
        man = tomllib.loads((REPO / sub / "pyproject.toml").read_text(encoding="utf-8"))
        got = set(man["project"].get("scripts", {}))
        if name == "gc_core":
            # gc-core — БИБЛИОТЕКА: команды в PATH не кладёт.
            assert not got, "gc-core не должен объявлять точки входа"
        else:
            assert got == expected, sub


def test_meta_pulls_every_profile():
    """Короткая команда `gc-ua @ git+URL` обязана давать полный агент: uv читает
    из git только КОРНЕВОЙ манифест, поэтому именно он определяет её состав."""
    root = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    ver = root["project"]["version"]
    assert root["project"]["name"] == "gc-ua"
    assert set(root["project"]["dependencies"]) == {
        f"gc-core=={ver}", f"gc-cli=={ver}", f"gc-tui=={ver}", f"gc-server=={ver}",
    }


def test_versions_are_pinned_together():
    """Профили обязаны собираться из одного коммита: пины друг на друга точные."""
    root = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    ver = root["project"]["version"]
    for sub in ("packages/core", "packages/cli", "packages/tui", "packages/server"):
        leaf = tomllib.loads((REPO / sub / "pyproject.toml").read_text(encoding="utf-8"))
        assert leaf["project"]["version"] == ver, f"{sub} разошёлся по версии"
    for sub in ("packages/cli", "packages/tui", "packages/server"):
        leaf = tomllib.loads((REPO / sub / "pyproject.toml").read_text(encoding="utf-8"))
        assert f"gc-core=={ver}" in leaf["project"]["dependencies"], sub


def test_profile_subdirs_match_layout():
    """app_version.PROFILE_SUBDIRS — источник правды для переустановки; он обязан
    совпадать с реальной раскладкой манифестов."""
    from gc_core import app_version
    assert app_version.PROFILE_SUBDIRS == {
        "gc-core": "packages/core",
        "gc-cli": "packages/cli",
        "gc-tui": "packages/tui",
        "gc-server": "packages/server",
    }
    for name, sub in app_version.PROFILE_SUBDIRS.items():
        man = tomllib.loads((REPO / sub / "pyproject.toml").read_text(encoding="utf-8"))
        assert man["project"]["name"] == name
