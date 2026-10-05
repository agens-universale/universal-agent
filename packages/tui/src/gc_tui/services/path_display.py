"""Адаптивное отображение пути в TUI — чистая презентация.

`truncate_path` (шапка): влезает целиком → весь путь; иначе сохраняем корень +
последний сегмент, середину схлопываем в `…`; совсем узко → `…<sep><last>`; хвост
длиннее max — обрезаем хвост.

`display_path` + `fit_path` (путь картинки под репликой): от папки проекта, через
`~` или полностью; в одну строку — серединой в `…`, а не влезает и так — одно имя.
Ширина — в символах: у путей с CJK строка может выйти длиннее."""

from __future__ import annotations

import os


def _sep(path: str) -> str:
    return "\\" if "\\" in path else "/"


def truncate_path(path: str, max_cols: int) -> str:
    if max_cols <= 0:
        return ""
    if len(path) <= max_cols:
        return path
    sep = _sep(path)
    leading = sep if path.startswith(sep) else ""
    nonempty = [p for p in path.split(sep) if p]
    if not nonempty:
        return path[-max_cols:]
    tail = nonempty[-1]
    minimal = f"{leading}…{sep}{tail}"
    if len(minimal) > max_cols:
        # даже хвост не влезает — показать конец хвоста с ведущим … (слайс
        # справа от "…"+tail терял сам символ …, когда tail длиннее max_cols)
        return "…" + tail[-(max_cols - 1) :] if max_cols > 1 else "…"
    # жадно наращиваем голову от корня, пока `<head><sep>…<sep><tail>` влезает
    best = minimal
    head_parts = nonempty[:-1]
    for k in range(1, len(head_parts) + 1):
        cand = f"{leading}{sep.join(head_parts[:k])}{sep}…{sep}{tail}"
        if len(cand) <= max_cols:
            best = cand
        else:
            break
    return best


def _inside(path: str, base: str) -> bool:
    try:
        return os.path.commonpath([path, base]) == os.path.normpath(base)
    except ValueError:  # разные диски Windows
        return False


def display_path(path: str, root: str, home: str) -> str:
    """Путь для показа: внутри `root` — от него, иначе внутри `home` — через `~`,
    иначе полностью. Не абсолютный (одно имя из старой сессии) — как есть."""
    if not os.path.isabs(path):
        return path
    if _inside(path, root):
        return os.path.relpath(path, root)
    if _inside(path, home):
        return os.path.join("~", os.path.relpath(path, home))
    return path


def fit_path(path: str, max_cols: int) -> str:
    """Путь в `max_cols` символов: целиком, иначе серединой в `…`
    (`truncate_path`); не влезает и так — одно имя файла, даже если оно длиннее."""
    fitted = truncate_path(path, max_cols)
    sep = _sep(path)
    name = path.rstrip(sep).rsplit(sep, 1)[-1]
    return fitted if fitted == path or fitted.endswith(sep + name) else name
