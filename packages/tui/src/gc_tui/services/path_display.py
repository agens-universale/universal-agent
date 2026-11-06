"""Адаптивное отображение пути в TUI — чистая презентация.

`fit_path` (путь картинки под репликой; сам путь для показа — ядро,
`display_path`): в одну строку — серединой в `…`, не влезает и так — одно
имя.
Ширина — в символах: у путей с CJK строка может выйти длиннее."""

from __future__ import annotations

from gc_core.utils.path_truncate import path_sep, truncate_path


def fit_path(path: str, max_cols: int) -> str:
    """Путь в `max_cols` символов: целиком, иначе серединой в `…`
    (`truncate_path`); не влезает и так — одно имя файла, даже если оно длиннее."""
    fitted = truncate_path(path, max_cols)
    sep = path_sep(path)
    name = path.rstrip(sep).rsplit(sep, 1)[-1]
    return fitted if fitted == path or fitted.endswith(sep + name) else name
