"""Пути к исходникам пакетов — единая точка для тестов.

До разбиения на модули тесты писали `root / "src" / "tools"`. Теперь исходники
разъехались по шести каталогам `packages/*/src`: пять поставляемых
(`{core,cli,tui,server}/src/gc_*` плюс `mcp/src/recommended_mcp`, у которого
своё имя, а не префикс `gc_`) и `testkit/src/gc_testkit`, который в колёса не
едет. Раскладка перестала быть очевидной из имени модуля, поэтому, чтобы при
следующем переезде правился один файл, а не сорок, все обращения идут через
этот модуль.

    from gc_testkit.paths import module_dir, REPO
    tools_dir = module_dir("tools")        # .../packages/core/src/gc_core/tools

Тест-кит ставится из рабочей копии, поэтому корень репозитория ищется вверх по
дереву по маркеру. Фиксированную глубину закладывать нельзя: у каждого пакета
свой каталог тестов, и запуск идёт и из корня, и из `packages/<имя>`.

Корень вычисляется ЛЕНИВО и на импорте модуля НЕ считается. Причина: пакет
объявляет `[project.entry-points.pytest11]`, поэтому его плагин подхватывается
автоматически КАЖДЫМ pytest в окружении, куда попало колесо gc-testkit. Пока
`REPO` считался жадно, а `_find_repo` бросал `RuntimeError`, установка колеса
обрывала чужой pytest ДО сбора тестов — воспроизведено. Теперь импорт модуля
не трогает файловую систему, а `RuntimeError` прилетает только тому, кто
реально спросил путь.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

#: Строгий маркер рабочей копии: `packages/core/pyproject.toml`, объявляющий
#: `name = "gc-core"`. Прежний маркер — «каталог packages/ рядом с
#: pyproject.toml» — ловил ЛЮБОЕ чужое монорепо: у проекта с `packages/*/src`
#: корнем объявлялся ЕГО корень, и пути тест-кита уезжали в чужое дерево.
#: tomllib здесь не используется намеренно: он в stdlib только с 3.11, а
#: gc-testkit объявляет requires-python >= 3.10. Проверить нужно ровно одну
#: строку, поэтому хватает регулярки.
_GC_CORE_NAME = re.compile(r'^[ \t]*name[ \t]*=[ \t]*["\']gc-core["\'][ \t]*$', re.MULTILINE)


def _declares_gc_core(candidate: Path) -> bool:
    """Лежит ли в `candidate` наш `packages/core/pyproject.toml`."""
    try:
        text = (candidate / "packages" / "core" / "pyproject.toml").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return _GC_CORE_NAME.search(text) is not None


def repo_root_from(start: Path) -> Path | None:
    """Корень рабочей копии вверх от `start`, иначе None.

    Реализация продублирована в `gc_core.bundled_assets` и
    `recommended_mcp.runtime.repo_path` — осознанно, а не по забывчивости: обе
    те точки входа (и эта в том числе) работают ДО того, как соседние
    дистрибутивы попадают на `sys.path`, поэтому импортировать общую
    реализацию физически нечем. Совпадение трёх копий стережёт
    `tests/test_repo_root_marker.py`.
    """
    for parent in Path(start).resolve().parents:
        if _declares_gc_core(parent):
            return parent
    return None


#: Корень, назначенный извне — см. `use_repo_root`. None = считать от `__file__`.
_SUITE_ROOT: Path | None = None


def use_repo_root(root: Path) -> None:
    """Назначить корень рабочей копии, которой принадлежит СОБИРАЕМЫЙ набор.

    Зовёт бутстрап плагина, узнав корень из `config.rootpath`. По умолчанию
    корень ищется вверх от `__file__`, то есть от места, откуда УСТАНОВЛЕН
    тест-кит, — и это верно ровно до тех пор, пока чекаут один. При двух
    (git worktree, второй клон, каталог ревью) venv из чекаута A, запущенный в
    чекауте B, разводил их: rootdir — B, а `REPO`, `PACKAGE_SRC_DIRS` и всё, что
    из них растёт (`import_health`, `module_literals`, сборка колёс), — A. Набор
    B зеленел, ни разу не посмотрев на исходники B.

    Ключом кто-либо считать не пытается: набор в прогоне ровно один, поэтому
    один корень на процесс — не упрощение, а факт устройства pytest.
    """
    global _SUITE_ROOT
    _SUITE_ROOT = Path(root).resolve()
    repo_root.cache_clear()
    # Ленивые константы могли уже осесть в globals() со старым корнем.
    for name in _LAZY_PATHS:
        globals().pop(name, None)


@lru_cache(maxsize=1)
def repo_root() -> Path | None:
    """Корень рабочей копии монорепозитория, иначе None. НЕ бросает."""
    if _SUITE_ROOT is not None:
        return _SUITE_ROOT
    return repo_root_from(Path(__file__))


def _require_repo() -> Path:
    """Корень или громкий отказ — для путей, которых вне рабочей копии нет."""
    root = repo_root()
    if root is None:
        raise RuntimeError(
            "gc_testkit: рабочая копия монорепозитория не найдена вверх от "
            f"{Path(__file__).resolve()} — нет packages/core/pyproject.toml с "
            'name = "gc-core". Пути тест-кита существуют только в рабочей копии.'
        )
    return root


def _src_dirs(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(p for p in root.glob("packages/*/src") if p.is_dir()))


def package_src_dirs() -> tuple[Path, ...]:
    """Каталоги `packages/*/src`; вне рабочей копии — пустой кортеж.

    Отличается от константы `PACKAGE_SRC_DIRS` тем, что НЕ бросает: её зовёт
    бутстрап `sys.path`, обязанный быть безвредным в постороннем окружении.
    """
    root = repo_root()
    return _src_dirs(root) if root is not None else ()


#: Ленивые константы модуля: считаются при ПЕРВОМ обращении и запоминаются в
#: globals(). PEP 562 — `from gc_testkit.paths import REPO` доходит до module
#: __getattr__, поэтому все существующие потребители (полтора десятка
#: тест-модулей, а также import_health и module_literals) работают без единой
#: правки, но импорт самого модуля больше ничего не считает и не может упасть.
#:
#: REPO             — корень рабочей копии;
#: ASSETS           — bundled-ассеты (prompts/, skills/, config.default.yml).
#:                    Принадлежат дистрибутиву gc-core и лежат рядом с его
#:                    исходниками, а не в корне, как было до разбиения;
#: PACKAGE_SRC_DIRS — каталоги исходников всех пакетов (то, что кладётся на
#:                    sys.path);
#: INTEGRATIONS,
#: HARBOR           — стенды бенчмарков: адаптеры Harbor и конфиги 2x2,
#:                    в колёса не попадают;
#: RECIPES          — курируемые рецепты MCP: дистрибутив gc-mcp;
#: VENDOR           — исходники MCP-серверов, которые ставит `gc-mcp install`;
#: TUI_PKG          — каталог пакета TUI, там же его стили (styles.tcss).
_LAZY_PATHS = {
    "REPO": lambda root: root,
    "ASSETS": lambda root: root / "packages" / "core" / "assets",
    "PACKAGE_SRC_DIRS": _src_dirs,
    "INTEGRATIONS": lambda root: root / "integrations",
    "HARBOR": lambda root: root / "integrations" / "harbor",
    "RECIPES": lambda root: root / "packages" / "mcp" / "src" / "recommended_mcp",
    "VENDOR": lambda root: root / "packages" / "mcp" / "external" / "vendor",
    "TUI_PKG": lambda root: root / "packages" / "tui" / "src" / "gc_tui",
}


def __getattr__(name: str):
    build = _LAZY_PATHS.get(name)
    if build is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = build(_require_repo())
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY_PATHS})


_DIST_OF = {"gc_core": "core", "gc_cli": "cli", "gc_tui": "tui", "gc_server": "server"}

# Пакеты, чьё содержимое развёрнуто в корень нового пакета: `tui/app.py` стал
# `gc_tui/app.py`, а не `gc_tui/tui/app.py`.
_FLATTENED = {"tui": "gc_tui"}


#: Карта «дореформенное плоское имя модуля → пакет». Лежит РЯДОМ с этим файлом,
#: а не в корне репозитория: единственный потребитель — тест-кит, и находить её
#: через вычисление корня значило бы повторять ошибку bundled_root().
_MAP_PATH = Path(__file__).resolve().parent / "module_map.json"


@lru_cache(maxsize=1)
def module_map() -> dict[str, str]:
    """Карта, прочитанная ЛЕНИВО и не более одного раза.

    Раньше здесь стоял `_MAP = _load_map()` — чтение и разбор JSON прямо на
    импорте модуля. Это ровно то, чего весь остальной файл избегает: пакет
    объявляет `[project.entry-points.pytest11]`, поэтому `gc_testkit.plugin`, а
    через него и этот модуль, грузится КАЖДЫМ pytest в окружении, куда попало
    колесо gc-testkit. `is_file()` спасал только от отсутствия файла: битое
    содержимое роняло импорт `JSONDecodeError`'ом ещё до сбора тестов, и чужой
    прогон умирал с сообщением, в котором имя gc-testkit не встречается вовсе.
    Воспроизведено подстановкой невалидного JSON в копию пакета.

    Повреждённая карта здесь не бросает: она означает «дореформенных имён не
    знаю». Кто действительно на неё опирается (`module_dir`), тот и откажет —
    с внятным текстом и путём к файлу.
    """
    if not _MAP_PATH.is_file():
        return {}
    try:
        return json.loads(_MAP_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def package_dir(package: str) -> Path:
    """Каталог пакета: ``gc_core`` -> ``packages/core/src/gc_core``."""
    return _require_repo() / "packages" / _DIST_OF[package] / "src" / package


def module_dir(old_top_level_name: str) -> Path:
    """Каталог модуля по его ДОРЕФОРМЕННОМУ имени верхнего уровня.

    Принимает старые имена (`tools`, `tui`, `mcp_runtime`), потому что именно
    ими тесты и оперируют: они собирают заглушки под теми же именами, что
    видит агент.
    """
    if old_top_level_name in _FLATTENED:
        return package_dir(_FLATTENED[old_top_level_name])
    pkg = module_map().get(old_top_level_name)
    if pkg is None:
        raise KeyError(f"неизвестный модуль {old_top_level_name!r}; проверьте {_MAP_PATH}")
    return package_dir(pkg) / old_top_level_name
