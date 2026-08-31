"""Контракт упаковки: шесть непересекающихся дистрибутивов из одного дерева.

До 0.4.0 колесо было одно, и тест проверял, что в нём лежит ВСЁ, включая
tui/styles.tcss. Потом tui, cli, server и менеджер MCP уехали в собственные
дистрибутивы, собираемые из того же дерева, и главный инвариант сменился:
колёса не должны заявлять права ни на один общий путь — иначе установка двух
профилей перетирает файлы, и чья версия победит, решает порядок распаковки.

Второй инвариант — версионный: профили пинят друг друга ТОЧНО, поэтому любой
манифест, разошедшийся по версии, обязан красить набор (см.
test_versions_are_pinned_together).
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
    "gc_core/assets/prompts/chat_completions_system_prompt.md",
    "gc_core/assets/prompts/chat_completions_tui_special_instructions.md",
    "gc_core/assets/config.default.yml",
    "gc_core/config.py",
]

# Ядро не должно содержать код фронтендов — ради этого всё и затевалось.
CORE_FORBIDDEN = ["gc_tui/", "gc_cli/", "gc_server/"]

MANIFESTS = {"gc_ua": ".", "gc_core": "packages/core", "gc_cli": "packages/cli",
             "gc_tui": "packages/tui", "gc_server": "packages/server",
             "gc_mcp": "packages/mcp"}


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
    assert any(n.startswith("gc_core/assets/skills/") for n in names), "skills/ отсутствует"


def test_mcp_wheel_bundles_recipes_and_sources(wheels):
    """Рецепты и исходники MCP едут в СВОЁМ дистрибутиве, а не в ядре.

    Соседство обязательно: install.py ищет исходники относительно каталога
    рецептов, поэтому в колесе они должны лежать так же, как в репозитории.
    """
    names = wheels["gc_mcp"]
    assert "recommended_mcp/config.yml" in names
    assert any(n.startswith("recommended_mcp/") and n.endswith(".py") for n in names)
    assert "external/vendor/ddgs-mcp/pyproject.toml" in names, "ddgs-mcp не поставляется"
    assert "external/vendor/ddgs-mcp/src/ddgs_mcp/server.py" in names

    core = wheels["gc_core"]
    assert not any(n.startswith("recommended_mcp/") for n in core), (
        "ядро больше не должно тащить рецепты"
    )


def test_core_wheel_does_not_squat_generic_names(wheels):
    """Дистрибутив не должен занимать родовые имена верхнего уровня.

    В Python 3 любой каталог на sys.path — namespace-пакет. Пока ассеты лежали
    рядом с пакетом, `import prompts` у встраивающего приложения молча
    резолвился в НАШИ промпты (проверено в чистом venv). Теперь они внутри
    gc_core/assets.

    recommended_mcp и external пока остаются: первый — настоящий импортируемый
    пакет, второй обязан быть его соседом. Их выносит отдельная задача.

    С 27.08.2026 ЯДРО их не импортирует вовсе: мост автозапуска daemon'ов уехал в
    сам дистрибутив рецептов (recommended_mcp.runtime.daemon_autostart), и прежняя
    оговорка «mcp_daemon_autostart тянет recommended_mcp.autostart» неверна.
    """
    squatted = {"prompts", "skills", "config.default.yml", "assets", "tests", "src"}
    for dist, names in wheels.items():
        tops = {n.split("/")[0] for n in names}
        hit = tops & squatted
        assert not hit, f"{dist} занимает родовые имена верхнего уровня: {sorted(hit)}"


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


#: Команды каждого дистрибутива. `uv tool install` экспортирует в PATH скрипты
#: ТОЛЬКО запрошенного дистрибутива, поэтому свою команду объявляет он сам.
OWN_COMMANDS = {
    "gc_cli": {"gc-cli"},
    "gc_tui": {"gc-ua", "gc-voice"},
    "gc_server": {"gc-serve"},
    "gc_mcp": {"gc-mcp", "gc-mcp-install"},
}


def test_each_distribution_declares_only_its_own_commands():
    """Ни одно имя команды не объявлено двумя дистрибутивами.

    Это не гигиена, а работоспособность: uv владеет симлинком ЭКСКЛЮЗИВНО.
    Пока gc-mcp/gc-mcp-install объявляли все три профиля, установка второго
    профиля падала с `error: Executables already exist`, а `uv tool uninstall`
    одного профиля уносил gc-mcp у другого. Проверено установкой 18.08.2026 —
    прежний комментарий «пересечение симлинка безвредно» был неверен.

    Мета gc-ua — исключение по построению: она тянет все профили и объявляет
    весь набор через собственный лаунчер. Ставить её вместе с отдельными
    профилями и не предполагается.
    """
    for name, sub in MANIFESTS.items():
        man = tomllib.loads((REPO / sub / "pyproject.toml").read_text(encoding="utf-8"))
        got = set(man["project"].get("scripts", {}))

        if name == "gc_core":
            # gc-core — БИБЛИОТЕКА: команды в PATH не кладёт.
            assert not got, f"{sub} — библиотека, точек входа быть не должно"
        elif name == "gc_ua":
            expected = set().union(*OWN_COMMANDS.values())
            assert got == expected, sub
        else:
            assert got == OWN_COMMANDS[name], sub


def test_command_names_do_not_collide_between_leaf_distributions():
    """Явно: два листовых дистрибутива не претендуют на одно имя в PATH."""
    seen: dict[str, str] = {}
    for name, sub in MANIFESTS.items():
        if name in ("gc_core", "gc_ua"):
            continue
        man = tomllib.loads((REPO / sub / "pyproject.toml").read_text(encoding="utf-8"))
        for cmd in man["project"].get("scripts", {}):
            assert cmd not in seen, (
                f"команду {cmd!r} объявляют и {seen[cmd]}, и {sub} — "
                f"uv откажется ставить второй дистрибутив"
            )
            seen[cmd] = sub





def test_meta_pulls_every_profile():
    """Короткая команда `gc-ua @ git+URL` обязана давать полный агент: uv читает
    из git только КОРНЕВОЙ манифест, поэтому именно он определяет её состав.

    Сравнение идёт с `ver` из этого же файла, то есть тест связывает зависимости
    корня с его СОБСТВЕННОЙ версией — и ничего не знает о листьях. Листья с
    корнем связывает сосед, `test_versions_are_pinned_together`; пока в его цикле
    не было packages/mcp, подмена версии менеджера ничего в этом файле не
    красила.
    """
    root = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    ver = root["project"]["version"]
    assert root["project"]["name"] == "gc-ua"
    assert set(root["project"]["dependencies"]) == {
        f"gc-core=={ver}", f"gc-cli=={ver}", f"gc-tui=={ver}", f"gc-server=={ver}",
        f"gc-mcp=={ver}",
    }


def test_versions_are_pinned_together():
    """Профили обязаны собираться из одного коммита: пины друг на друга точные.

    packages/mcp тут не ради полноты списка. Пока его в цикле не было, подмена
    его версии не красила В ЭТОМ ФАЙЛЕ ничего: при 0.5.1 у менеджера и 0.5.0 у
    остальных манифестов прочие 14 тестов файла проходили (проверено подменой
    28.08.2026). Разработчик поломки тоже не видит — на рабочем дереве её гасит
    `[tool.uv.sources] gc-mcp = { workspace = true }`, и `uv sync` спокойно ставит
    менеджера 0.5.1 рядом с gc-tui, который пинит 0.5.0.

    А на КОЛЁСНОМ пути, по которому едет пользователь, поломка настоящая:
    METADATA собранного gc_tui-0.5.0 несёт `Requires-Dist: gc-mcp==0.5.0`, в
    выпуске лежит gc_mcp-0.5.1, и `uv pip install --no-index` отвечает «No
    solution found ... gc-tui==0.5.0 cannot be used» (тоже проверено).
    """
    root = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    ver = root["project"]["version"]
    for sub in ("packages/core", "packages/cli", "packages/tui", "packages/server",
                "packages/mcp"):
        leaf = tomllib.loads((REPO / sub / "pyproject.toml").read_text(encoding="utf-8"))
        assert leaf["project"]["version"] == ver, f"{sub} разошёлся по версии"
    for sub in ("packages/cli", "packages/tui", "packages/server", "packages/mcp"):
        leaf = tomllib.loads((REPO / sub / "pyproject.toml").read_text(encoding="utf-8"))
        assert f"gc-core=={ver}" in leaf["project"]["dependencies"], sub
    # Менеджер MCP — жёсткая зависимость профилей-приложений: команды gc-mcp/
    # gc-mcp-install объявляет он один (fba7c20b), и без точного пина профиль
    # соберётся с менеджером из другого коммита. Ядро на него не зависит —
    # зависимость обратная, gc-mcp тянет gc-core.
    for sub in ("packages/cli", "packages/tui", "packages/server"):
        leaf = tomllib.loads((REPO / sub / "pyproject.toml").read_text(encoding="utf-8"))
        assert f"gc-mcp=={ver}" in leaf["project"]["dependencies"], sub


def test_profile_subdirs_match_layout():
    """app_version.PROFILE_SUBDIRS — источник правды для переустановки; он обязан
    совпадать с реальной раскладкой манифестов.

    Мета `gc-ua` в словаре отсутствует НАМЕРЕННО: её манифест лежит в корне, и
    `#subdirectory=` ей не нужен. Поэтому расхождение между PROFILE_DISTS и
    PROFILE_SUBDIRS проверяем явно, ровно на одну запись — иначе следующая
    правка списка профилей снова оставит install_hint без подкаталога и без
    сторожа.
    """
    from gc_core import app_version
    assert app_version.PROFILE_SUBDIRS == {
        "gc-core": "packages/core",
        "gc-cli": "packages/cli",
        "gc-tui": "packages/tui",
        "gc-server": "packages/server",
        "gc-mcp": "packages/mcp",
    }
    assert set(app_version.PROFILE_DISTS) - set(app_version.PROFILE_SUBDIRS) == {"gc-ua"}
    for name, sub in app_version.PROFILE_SUBDIRS.items():
        man = tomllib.loads((REPO / sub / "pyproject.toml").read_text(encoding="utf-8"))
        assert man["project"]["name"] == name
