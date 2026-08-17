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
    "config.py",          # src/ разворачивается в верхний уровень
    "tui_entry.py",       # шим точки входа gc-ua обязан быть в ядре
    "serve_entry.py",
]

CORE_FORBIDDEN = ["tui/", "diff_tui/", "rest_api/", "swarm/", "server.py",
                  "server_cleanup_threads.py", "__init__.py", ".gitignore"]

MANIFESTS = {"gc_ua": ".", "gc_cli": "packaging/cli",
             "gc_tui": "packaging/tui", "gc_server": "packaging/server"}


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


def test_three_distributions_built(wheels):
    assert set(wheels) == set(MANIFESTS), sorted(wheels)


def test_wheels_are_pairwise_disjoint(wheels):
    """Главный инвариант разделения: ни одного общего пути между колёсами."""
    for a, b in itertools.combinations(sorted(wheels), 2):
        overlap = wheels[a] & wheels[b]
        assert not overlap, f"{a} и {b} заявляют общие пути: {sorted(overlap)[:10]}"


def test_core_wheel_bundles_assets(wheels):
    names = wheels["gc_cli"]
    for needed in CORE_REQUIRED:
        assert needed in names, f"{needed} нет в колесе ядра; образец {sorted(names)[:10]}"
    assert any(n.startswith("skills/") for n in names), "skills/ отсутствует"
    assert any(n.startswith("recommended_mcp/") and n.endswith(".py") for n in names)
    assert "external/ddgs-mcp/pyproject.toml" in names, "external/ddgs-mcp не поставляется"
    assert "external/ddgs-mcp/src/ddgs_mcp/server.py" in names


def test_core_wheel_excludes_frontends(wheels):
    """Ради этого всё и затевалось: headless-установка не должна класть на диск
    код фронтендов."""
    names = wheels["gc_cli"]
    for bad in CORE_FORBIDDEN:
        hit = [n for n in names if n == bad or n.startswith(bad)]
        assert not hit, f"{bad} просочился в колесо ядра: {hit[:5]}"


def test_tui_wheel_contents(wheels):
    names = wheels["gc_tui"]
    assert {n.split("/")[0] for n in names} == {"tui", "diff_tui"}, sorted(names)[:10]
    assert "tui/styles.tcss" in names, "стили TUI обязаны ехать вместе с пакетом"


def test_server_wheel_contents(wheels):
    names = wheels["gc_server"]
    assert {n.split("/")[0] for n in names} == {
        "server.py", "server_cleanup_threads.py", "rest_api", "swarm",
    }, sorted(names)


def test_every_profile_declares_all_commands():
    """`uv tool install` экспортирует в PATH скрипты ТОЛЬКО запрошенного
    дистрибутива. Поэтому команды объявляет каждый профиль — иначе установка
    листа дала бы окружение вообще без команд (проверено живьём)."""
    expected = {"gc-cli", "gc-ua", "gc-serve", "gc-mcp", "gc-mcp-install"}
    for sub in MANIFESTS.values():
        man = tomllib.loads((REPO / sub / "pyproject.toml").read_text(encoding="utf-8"))
        assert set(man["project"].get("scripts", {})) == expected, sub


def test_meta_pulls_every_profile():
    """Короткая команда `gc-ua @ git+URL` обязана давать полный агент: uv читает
    из git только КОРНЕВОЙ манифест, поэтому именно он определяет её состав."""
    root = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    ver = root["project"]["version"]
    assert root["project"]["name"] == "gc-ua"
    assert set(root["project"]["dependencies"]) == {
        f"gc-cli=={ver}", f"gc-tui=={ver}", f"gc-server=={ver}",
    }


def test_versions_are_pinned_together():
    """Профили обязаны собираться из одного коммита: пины друг на друга точные."""
    root = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    ver = root["project"]["version"]
    for sub in ("packaging/cli", "packaging/tui", "packaging/server"):
        leaf = tomllib.loads((REPO / sub / "pyproject.toml").read_text(encoding="utf-8"))
        assert leaf["project"]["version"] == ver, f"{sub} разошёлся по версии"
    for sub in ("packaging/tui", "packaging/server"):
        leaf = tomllib.loads((REPO / sub / "pyproject.toml").read_text(encoding="utf-8"))
        assert f"gc-cli=={ver}" in leaf["project"]["dependencies"], sub


def test_profile_subdirs_match_layout():
    """app_version.PROFILE_SUBDIRS — источник правды для переустановки; он обязан
    совпадать с реальной раскладкой манифестов."""
    import app_version
    assert app_version.PROFILE_SUBDIRS == {
        "gc-cli": "packaging/cli",
        "gc-tui": "packaging/tui",
        "gc-server": "packaging/server",
    }
    for name, sub in app_version.PROFILE_SUBDIRS.items():
        man = tomllib.loads((REPO / sub / "pyproject.toml").read_text(encoding="utf-8"))
        assert man["project"]["name"] == name
