"""Путь для показа человеку: от корня проекта, через `~` или полностью.

Чистая функция: корень и домашняя папка приходят от вызывающего."""

from __future__ import annotations

import os

# Модуль путей платформы; тесты подменяют его на ``ntpath``.
_path = os.path


def _inside(path: str, base: str) -> bool:
    try:
        return _path.commonpath([path, base]) == _path.normpath(base)
    except ValueError:  # разные диски Windows
        return False


def display_path(path: str, root: str, home: str) -> str:
    """Внутри `root` — от него, иначе внутри `home` — через `~`, иначе
    полностью. Не абсолютный (уже от корня или имя из старой сессии) — как
    есть."""
    if not _path.isabs(path):
        return path
    if _inside(path, root):
        return _path.relpath(path, root)
    if _inside(path, home):
        return _path.join("~", _path.relpath(path, home))
    return path
