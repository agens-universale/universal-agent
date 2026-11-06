"""Адаптивное отображение пути в TUI — чистая презентация.

`display_path` + `fit_path` (путь картинки под репликой): от папки проекта, через
`~` или полностью; в одну строку — серединой в `…`, а не влезает и так — одно имя.
Ширина — в символах: у путей с CJK строка может выйти длиннее."""

from __future__ import annotations

import os

from gc_core.utils.path_truncate import path_sep, truncate_path


def _inside(path: str, base: str) -> bool:
    try:
        return os.path.commonpath([path, base]) == os.path.normpath(base)
    except ValueError:  # разные диски Windows
        return False


def home_path(path: str, home: str) -> str:
    """Путь внутри `home` — через `~`, иначе как есть."""
    if os.path.isabs(path) and _inside(path, home):
        return os.path.join("~", os.path.relpath(path, home))
    return path


def display_path(path: str, root: str, home: str) -> str:
    """Путь для показа: внутри `root` — от него, иначе внутри `home` — через `~`,
    иначе полностью. Не абсолютный (одно имя из старой сессии) — как есть."""
    if os.path.isabs(path) and _inside(path, root):
        return os.path.relpath(path, root)
    return home_path(path, home)


def fit_path(path: str, max_cols: int) -> str:
    """Путь в `max_cols` символов: целиком, иначе серединой в `…`
    (`truncate_path`); не влезает и так — одно имя файла, даже если оно длиннее."""
    fitted = truncate_path(path, max_cols)
    sep = path_sep(path)
    name = path.rstrip(sep).rsplit(sep, 1)[-1]
    return fitted if fitted == path or fitted.endswith(sep + name) else name
