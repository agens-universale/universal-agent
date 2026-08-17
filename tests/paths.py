"""Пути к исходникам пакетов — единая точка для тестов.

До разбиения на модули тесты писали `root / "src" / "tools"`. Теперь исходники
разъехались по четырём пакетам (`packages/{core,cli,tui,server}/src/gc_*`), и
раскладка перестала быть очевидной из имени модуля. Чтобы при следующем
переезде правился один файл, а не сорок, все обращения идут через этот модуль.

    from tests.paths import module_dir, REPO
    tools_dir = module_dir("tools")        # .../packages/core/src/gc_core/tools
"""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: Каталог bundled-ассетов (prompts/, skills/, config.default.yml). Они
#: принадлежат дистрибутиву gc-core и живут рядом с его исходниками, а не в
#: корне репозитория, как было до разбиения на пакеты.
ASSETS = REPO / "packages" / "core" / "assets"

#: Каталоги исходников всех пакетов — то, что кладётся на ``sys.path``.
PACKAGE_SRC_DIRS = tuple(sorted(p for p in REPO.glob("packages/*/src") if p.is_dir()))

#: Каталог пакета TUI — там же лежат его стили (styles.tcss).
TUI_PKG = REPO / "packages" / "tui" / "src" / "gc_tui"

_DIST_OF = {"gc_core": "core", "gc_cli": "cli", "gc_tui": "tui", "gc_server": "server"}

# Пакеты, чьё содержимое развёрнуто в корень нового пакета: `tui/app.py` стал
# `gc_tui/app.py`, а не `gc_tui/tui/app.py`.
_FLATTENED = {"tui": "gc_tui"}


def _load_map() -> dict[str, str]:
    p = REPO / "tools_dev" / "module_map.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


_MAP = _load_map()


def package_dir(package: str) -> Path:
    """Каталог пакета: ``gc_core`` -> ``packages/core/src/gc_core``."""
    return REPO / "packages" / _DIST_OF[package] / "src" / package


def module_dir(old_top_level_name: str) -> Path:
    """Каталог модуля по его ДОРЕФОРМЕННОМУ имени верхнего уровня.

    Принимает старые имена (`tools`, `tui`, `mcp_runtime`), потому что именно
    ими тесты и оперируют: они собирают заглушки под теми же именами, что
    видит агент.
    """
    if old_top_level_name in _FLATTENED:
        return package_dir(_FLATTENED[old_top_level_name])
    pkg = _MAP.get(old_top_level_name)
    if pkg is None:
        raise KeyError(
            f"неизвестный модуль {old_top_level_name!r}; "
            f"проверьте tools_dev/module_map.json"
        )
    return package_dir(pkg) / old_top_level_name
