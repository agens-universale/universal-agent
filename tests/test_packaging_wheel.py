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
import os
import re
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
    "gc_core/assets/prompts/chat_completions_responses_special_instructions.md",
    "gc_core/assets/prompts/chat_completions_completions_special_instructions.md",
    "gc_core/assets/config.default.yml",
    "gc_core/config.py",
]

# Ядро не должно содержать код фронтендов — ради этого всё и затевалось.
CORE_FORBIDDEN = ["gc_tui/", "gc_cli/", "gc_server/"]

MANIFESTS = {
    "gc_ua": ".",
    "gc_core": "packages/core",
    "gc_cli": "packages/cli",
    "gc_tui": "packages/tui",
    "gc_server": "packages/server",
    "gc_mcp": "packages/mcp",
    "gc_chat": "packages/chat",
}


def _build_all(out: Path) -> dict:
    for sub in MANIFESTS.values():
        r = subprocess.run(
            ["uv", "build", "--wheel", "-o", str(out), sub],
            cwd=str(REPO),
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, r.stderr
    wheels = {}
    for w in out.glob("*.whl"):
        key = w.name.split("-")[0]
        names = set(zipfile.ZipFile(w).namelist())
        wheels[key] = {
            n for n in names if not n.split("/")[0].endswith(".dist-info")
        }
    return wheels


@pytest.fixture(scope="module")
def _dist_dir(tmp_path_factory):
    """Каталог со собранными колёсами. Сборка шести колёс дорогая, поэтому она
    одна на модуль, а `wheels`/`wheel_infos` — два взгляда на один результат."""
    if shutil.which("uv") is None:
        pytest.skip("uv not on PATH")
    return tmp_path_factory.mktemp("dist")


@pytest.fixture(scope="module")
def wheels(_dist_dir):
    return _build_all(_dist_dir)


@pytest.fixture(scope="module")
def wheel_infos(_dist_dir, wheels):
    """`ZipInfo` по каждому файлу колеса — нужен там, где важны права доступа.

    Зависит от `wheels`, чтобы сборка гарантированно случилась: сам по себе
    `_dist_dir` пуст.
    """
    infos = {}
    for w in _dist_dir.glob("*.whl"):
        key = w.name.split("-")[0]
        infos[key] = {i.filename: i for i in zipfile.ZipFile(w).infolist()}
    return infos


def test_all_distributions_built(wheels):
    assert set(wheels) == set(MANIFESTS), sorted(wheels)


def test_wheels_are_pairwise_disjoint(wheels):
    """Главный инвариант разделения: ни одного общего пути между колёсами."""
    for a, b in itertools.combinations(sorted(wheels), 2):
        overlap = wheels[a] & wheels[b]
        assert not overlap, (
            f"{a} и {b} заявляют общие пути: {sorted(overlap)[:10]}"
        )


def test_core_wheel_bundles_assets(wheels):
    names = wheels["gc_core"]
    for needed in CORE_REQUIRED:
        assert needed in names, (
            f"{needed} нет в колесе ядра; образец {sorted(names)[:10]}"
        )
    assert any(n.startswith("gc_core/assets/skills/") for n in names), (
        "skills/ отсутствует"
    )


def test_mcp_wheel_bundles_recipes_and_sources(wheels):
    """Рецепты и исходники MCP едут в СВОЁМ дистрибутиве, а не в ядре.

    Соседство обязательно: install.py ищет исходники относительно каталога
    рецептов, поэтому в колесе они должны лежать так же, как в репозитории.
    """
    names = wheels["gc_mcp"]
    assert "recommended_mcp/config.yml" in names
    assert any(
        n.startswith("recommended_mcp/") and n.endswith(".py") for n in names
    )
    # Внутри пакета, а не рядом: наверху `external` занимал бы родовое имя —
    # см. test_core_wheel_does_not_squat_generic_names.
    assert (
        "recommended_mcp/external/vendor/ddgs-mcp/pyproject.toml" in names
    ), "ddgs-mcp не поставляется"
    assert (
        "recommended_mcp/external/vendor/ddgs-mcp/src/ddgs_mcp/server.py"
        in names
    )

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

    `external` уехал внутрь пакета-владельца (`recommended_mcp/external/vendor/`)
    03.09.2026 — по той же причине и тем же приёмом, что и ассеты ядра. Пока он
    лежал наверху, проект со своим `external/` получал склейку путей
    (`import external` → обе портянки), а с `external/__init__.py` — наоборот,
    затирал наш каталог. Оба варианта воспроизведены в чистом venv.

    `recommended_mcp` остаётся наверху законно: это настоящий импортируемый
    пакет, имя дистрибутива.

    С 27.08.2026 ЯДРО их не импортирует вовсе: мост автозапуска daemon'ов уехал в
    сам дистрибутив рецептов (recommended_mcp.runtime.daemon_autostart), и прежняя
    оговорка «mcp_daemon_autostart тянет recommended_mcp.autostart» неверна.
    """
    squatted = {
        "prompts",
        "skills",
        "config.default.yml",
        "assets",
        "tests",
        "src",
        # external — вендоры MCP: с 03.09.2026 внутри recommended_mcp/.
        "external",
    }
    for dist, names in wheels.items():
        tops = {n.split("/")[0] for n in names}
        hit = tops & squatted
        assert not hit, (
            f"{dist} занимает родовые имена верхнего уровня: {sorted(hit)}"
        )


def test_testkit_never_ships(wheels):
    """Тест-кит — инструмент разработки, в поставку он попадать не должен.

    gc_testkit лежит в packages/ рядом с настоящими пакетами и подхватывается
    workspace-ом, поэтому единственная защита от его утечки — проверка колёс.
    """
    for dist, names in wheels.items():
        leaked = [n for n in names if n.split("/")[0] == "gc_testkit"]
        assert not leaked, (
            f"{dist} тащит тестовую инфраструктуру: {leaked[:5]}"
        )


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
    assert "gc_tui/styles.tcss" in names, (
        "стили TUI обязаны ехать вместе с пакетом"
    )
    assert "gc_tui/diff_tui/tracking.py" in names, (
        "diff_tui — часть TUI-профиля"
    )


def test_server_wheel_contents(wheels):
    names = wheels["gc_server"]
    assert {n.split("/")[0] for n in names} == {"gc_server"}, sorted(names)[
        :10
    ]
    for m in (
        "gc_server/server.py",
        "gc_server/rest_api/__init__.py",
        "gc_server/swarm/runner.py",
    ):
        assert m in names, m


def test_vendored_ddgs_ships_without_build_junk(wheels):
    """В колесо едут только версионированные файлы вендоренного ddgs-mcp.

    `force-include`, нацеленный на КАТАЛОГ, забирает его целиком — вместе с
    `build/lib/**`, `src/*.egg-info/**` и `uv.lock`, которых нет под гитом: они
    появляются у любого, кто хоть раз собирал ddgs-mcp локально. Состав колеса
    начинал зависеть от истории машины сборщика (было 8 лишних записей из 15),
    а воспроизводимость сборки — ровно то, ради чего колесо и собирают.

    Подмена, которую ловит тест: вернуть цель каталогом
    (`"external/vendor/ddgs-mcp" = "recommended_mcp/external/vendor/ddgs-mcp"`)
    на копии, где ddgs-mcp собирали.
    """
    shipped = {
        n
        for n in wheels["gc_mcp"]
        if n.startswith("recommended_mcp/external/")
    }
    assert shipped, "исходники ddgs-mcp вообще не попали в колесо"

    junk = {
        n
        for n in shipped
        if "/build/" in n
        or ".egg-info" in n
        or n.endswith(("uv.lock", ".pyc"))
    }
    assert not junk, f"в колесо уехали артефакты сборки: {sorted(junk)}"

    # Достаточность: без манифеста и исходников `pip install <path>` не соберёт
    # пакет, и локальная установка тихо свалится в git-метод.
    for needed in (
        "recommended_mcp/external/vendor/ddgs-mcp/pyproject.toml",
        "recommended_mcp/external/vendor/ddgs-mcp/src/ddgs_mcp/__init__.py",
    ):
        assert needed in shipped, (
            f"{needed} нет в колесе; есть {sorted(shipped)}"
        )


def test_server_wheel_ships_the_sandbox_wrapper(wheels, wheel_infos):
    """Обёртка песочницы едет в колесе, и с исполняемым битом.

    На этом файле держится весь swarm в установленном профиле: `runner` зовёт
    его через Popen. До разбиения на пакеты обёртка лежала в `scripts/`, в
    колёса не попадала вовсе, и swarm из установки не работал в принципе —
    стеречь было нечего. Теперь это package data, и промах будет виден только
    пользователю, на первом POST /swarm/run: остальной набор останется зелёным,
    потому что ни один тест не заглядывает внутрь колеса за этим файлом.

    Бит +x проверяется отдельно от наличия: без него Popen даёт PermissionError,
    а не FileNotFoundError, и диагноз выглядит совсем иначе.
    """
    wrapper = "gc_server/swarm/run-cli-in-sandbox.sh"
    assert wrapper in wheels["gc_server"], (
        f"{wrapper} не попал в колесо — swarm в установленном профиле не поднимется: runner зовёт этот файл через Popen"
    )

    if os.name == "nt":
        # Бит +x hatchling берёт с диска, а у файлов на Windows его нет: колесо,
        # собранное здесь, едет без него. Swarm на Windows не работает вовсе, а
        # пользователь на Linux собирает колесо у себя, из своего клона.
        pytest.skip("бит +x на Windows не хранится — проверка только на POSIX")
    mode = wheel_infos["gc_server"][wrapper].external_attr >> 16
    assert mode & 0o111, (
        f"{wrapper} едет без исполняемого бита (режим {oct(mode)}) — Popen даст PermissionError"
    )


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
    "gc_chat": {"gc-chat"},
}


def test_each_distribution_declares_only_its_own_commands():
    """Ни одно имя команды не объявлено двумя ЛИСТОВЫМИ дистрибутивами.

    Это не гигиена, а работоспособность: uv владеет симлинком ЭКСКЛЮЗИВНО.
    Пока gc-mcp/gc-mcp-install объявляли все три профиля, установка второго
    профиля падала с `error: Executables already exist`, а `uv tool uninstall`
    одного профиля уносил gc-mcp у другого. Проверено установкой 18.08.2026 —
    прежний комментарий «пересечение симлинка безвредно» был неверен.

    Мета gc-ua из проверки исключена, и это НЕ значит, что она ни с чем не
    пересекается: она объявляет ровно объединение команд листьев, то есть
    пересекается с каждым. Прежний заголовок этого докстринга утверждал
    «ни одно имя не объявлено двумя дистрибутивами» — неправда, и сам тест об
    этом знает: ветка `elif name == "gc_ua"` требует именно объединения.

    Убрать блок у меты нельзя: своих файлов у неё нет (`bypass-selection`), а
    `uv tool install` выносит наружу только скрипты ЗАПРОШЕННОГО дистрибутива —
    без `[project.scripts]` полный профиль дал бы НОЛЬ команд. Проверено
    установкой.

    Следствие принято сознательно: мету и любой листовой дистрибутив рядом не
    поставить, uv ответит `Executables already exist`. Лечится `--force`; после
    него на команду претендуют два инструмента, и `uv tool uninstall gc-mcp`
    унесёт её у полного профиля. Оговорка — в docs/usage.md.

    Прежнее оправдание ссылалось на «собственный лаунчер» меты; лаунчера нет с
    71abf728, мета стала чисто метаданными.
    """
    for name, sub in MANIFESTS.items():
        man = tomllib.loads(
            (REPO / sub / "pyproject.toml").read_text(encoding="utf-8")
        )
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
        man = tomllib.loads(
            (REPO / sub / "pyproject.toml").read_text(encoding="utf-8")
        )
        for cmd in man["project"].get("scripts", {}):
            assert cmd not in seen, (
                f"команду {cmd!r} объявляют и {seen[cmd]}, и {sub} — uv откажется ставить второй дистрибутив"
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
        f"gc-core=={ver}",
        f"gc-cli=={ver}",
        f"gc-tui=={ver}",
        f"gc-server=={ver}",
        f"gc-mcp=={ver}",
        f"gc-chat=={ver}",
    }


def test_versions_are_pinned_together():
    """Профили обязаны собираться из одного коммита: пины друг на друга точные.

    packages/mcp тут не ради полноты списка. Пока его в цикле не было, подмена
    его версии не красила В ЭТОМ ФАЙЛЕ ничего: при 0.3.1 у менеджера и 0.3.0 у
    остальных манифестов прочие 14 тестов файла проходили (проверено подменой
    28.08.2026). Разработчик поломки тоже не видит — на рабочем дереве её гасит
    `[tool.uv.sources] gc-mcp = { workspace = true }`, и `uv sync` спокойно ставит
    менеджера 0.3.1 рядом с gc-tui, который пинит 0.3.0.

    А на КОЛЁСНОМ пути, по которому едет пользователь, поломка настоящая:
    METADATA собранного gc_tui-0.3.0 несёт `Requires-Dist: gc-mcp==0.3.0`, в
    выпуске лежит gc_mcp-0.3.1, и `uv pip install --no-index` отвечает «No
    solution found ... gc-tui==0.3.0 cannot be used» (тоже проверено).
    """
    root = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    ver = root["project"]["version"]
    for sub in (
        "packages/core",
        "packages/cli",
        "packages/tui",
        "packages/server",
        "packages/mcp",
        "packages/chat",
    ):
        leaf = tomllib.loads(
            (REPO / sub / "pyproject.toml").read_text(encoding="utf-8")
        )
        assert leaf["project"]["version"] == ver, f"{sub} разошёлся по версии"
    for sub in (
        "packages/cli",
        "packages/tui",
        "packages/server",
        "packages/mcp",
        "packages/chat",
    ):
        leaf = tomllib.loads(
            (REPO / sub / "pyproject.toml").read_text(encoding="utf-8")
        )
        assert f"gc-core=={ver}" in leaf["project"]["dependencies"], sub
    # Менеджер MCP — жёсткая зависимость профилей-приложений: команды gc-mcp/
    # gc-mcp-install объявляет он один (fba7c20b), и без точного пина профиль
    # соберётся с менеджером из другого коммита. Ядро на него не зависит —
    # зависимость обратная, gc-mcp тянет gc-core.
    for sub in ("packages/cli", "packages/tui", "packages/server"):
        leaf = tomllib.loads(
            (REPO / sub / "pyproject.toml").read_text(encoding="utf-8")
        )
        assert f"gc-mcp=={ver}" in leaf["project"]["dependencies"], sub
    # gc-cli — зависимость СЕРВЕРА, а не гигиена списка: sworm запускает внутри
    # песочницы именно `gc-cli` (swarm/run-cli-in-sandbox.sh). Пока дистрибутив
    # был один, он объявлял обе команды сам; после разделения профилей установка
    # сервера перестала приносить CLI, и каждый sworm заканчивался exit 127.
    # Пин обязан быть ТОЧНЫМ, как у соседей: с `>=` в установку сервера приедет
    # CLI из другого коммита, и материализованный для песочницы config.yml
    # разойдётся со схемой ядра, которое стоит рядом. Подмена `==` на `>=` этой
    # строкой и красится — цикл выше её не держит, он смотрит только на gc-core
    # и gc-mcp.
    server = tomllib.loads(
        (REPO / "packages/server/pyproject.toml").read_text(encoding="utf-8")
    )
    assert f"gc-cli=={ver}" in server["project"]["dependencies"], (
        "gc-server обязан пинить gc-cli точно — иначе sworm остаётся без CLI или получает его из чужого коммита"
    )


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
    assert set(app_version.PROFILE_DISTS) - set(
        app_version.PROFILE_SUBDIRS
    ) == {"gc-ua"}
    for name, sub in app_version.PROFILE_SUBDIRS.items():
        man = tomllib.loads(
            (REPO / sub / "pyproject.toml").read_text(encoding="utf-8")
        )
        assert man["project"]["name"] == name


def test_force_include_keys_stay_inside_project_root():
    """Ключи `force-include` не уходят за каталог своего пакета.

    Ключ вида `../../external/…` работает при сборке из рабочего дерева и молча
    ломает сборку колеса ИЗ sdist: hatchling ищет исходник относительно корня
    распакованного архива, а там `../..` ведёт наружу, где ничего нет. Симптом —
    `FileNotFoundError: Forced include not found`, и падает он ПОСЛЕ того, как
    остальные пакеты собрались: `uv build --all-packages` оставляет tar.gz без
    колеса, то есть частичный релиз, с виду похожий на полный.

    Ровно из-за такого ключа вендоренные MCP-серверы переехали из
    `external/vendor/` внутрь `packages/mcp/external/vendor/`: там оба контекста
    совпадают — и рабочее дерево, и архив видят один и тот же путь.

    Подмена, которую ловит тест: вернуть любому ключу префикс `../`.
    """
    offenders = []
    for _name, sub in MANIFESTS.items():
        man = tomllib.loads(
            (REPO / sub / "pyproject.toml").read_text(encoding="utf-8")
        )
        targets = (
            man.get("tool", {})
            .get("hatch", {})
            .get("build", {})
            .get("targets", {})
            .get("wheel", {})
            .get("force-include", {})
        )
        for key in targets:
            if key.startswith("..") or Path(key).is_absolute():
                offenders.append(f"{sub}/pyproject.toml: {key}")

    assert not offenders, (
        "force-include ведёт за пределы каталога пакета — колесо из sdist не соберётся:\n  "
        + "\n  ".join(offenders)
    )


def test_vendored_sources_live_inside_their_package():
    """Исходники вендоренных MCP лежат внутри пакета-владельца.

    Не косметика: пока они лежали в `external/vendor/` корня, единственный
    способ положить их в колесо — ключ с `../../`, а он и ломает sdist (см.
    тест выше). Плюс `VENDOR_REL` обслуживал два разных смысла сразу.

    Подмена: вернуть каталог в корень репозитория.
    """
    inside = REPO / "packages" / "mcp" / "external" / "vendor"
    assert inside.is_dir(), f"вендоры не найдены в {inside}"

    names = sorted(p.name for p in inside.iterdir() if p.is_dir())
    assert len(names) >= 4, f"ожидалось четыре вендора, найдено: {names}"

    stale = REPO / "external" / "vendor"
    assert not stale.exists(), (
        f"{stale} существует — переезд сделан наполовину, и два места будут расходиться"
    )


def test_docs_never_offer_bare_pip_for_our_distributions():
    """Документация не должна советовать `pip install` для наших пакетов.

    Манифесты профилей пинят листья (`gc-core==0.3.0` и соседей), а те
    резолвятся через `[tool.uv.sources] workspace = true` и на PyPI не
    публикуются. pip этой секции не понимает и падает
    `No matching distribution found for gc-cli==0.3.0` — это описано в
    docs/usage.md, раздел «Через pip — не работает».

    Заведено после того, как docs/swarm.md пережил переезд с
    `pip install -e .` (на develop это работало: дистрибутив был один) на
    `pip install "gc-server @ git+…#subdirectory=packages/server"` — команду,
    которая не работает вовсе. Соседняя строка комментария при этом честно
    сообщала, что pip не годится. Проверено запуском обеих команд: pip падает,
    `uv pip install` того же спека ставит профиль вместе с gc-cli, gc-mcp,
    psutil и uvicorn.

    Ошибка такого рода не ловится ничем: доку никто не исполняет, а человек,
    который по ней ставит сервер, упирается в ошибку резолва и не понимает,
    он что-то сделал не так или инструкция врёт.
    """
    # Только файлы под гитом: документация — то, что в репозитории. Черновики в
    # рабочей копии (неотслеживаемые заметки) красили тест у их владельца.
    tracked = (
        subprocess.run(
            ["git", "ls-files", "-z", "--", "docs/*.md", "readme.md"],
            cwd=REPO,
            capture_output=True,
            check=True,
        )
        .stdout.decode("utf-8")
        .split("\0")
    )
    offenders = []
    for doc in (REPO / rel for rel in sorted(filter(None, tracked))):
        if not doc.is_file():
            continue
        for num, line in enumerate(
            doc.read_text(encoding="utf-8").splitlines(), 1
        ):
            stripped = line.strip()
            # Только предлагаемые КОМАНДЫ. Строки, которые про pip рассказывают
            # («pip install -e . падает»), обязаны остаться: они и объясняют,
            # почему так нельзя.
            if stripped.startswith(("#", ">", "*", "-", "`pip", "|")):
                continue
            if "pip install" not in stripped or "uv pip install" in stripped:
                continue
            if re.search(
                r"\bgc-(core|cli|tui|server|mcp|ua|testkit)\b", stripped
            ):
                offenders.append(f"{doc.relative_to(REPO)}:{num}: {stripped}")

    assert not offenders, (
        "в документации предлагается ставить наш дистрибутив обычным pip — "
        "он не резолвит листья workspace и упадёт:\n  "
        + "\n  ".join(offenders)
    )
