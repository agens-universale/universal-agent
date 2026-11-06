"""Сокращение пути по ширине: корень + последний сегмент, середина в `…`.

Влезает целиком → весь путь; иначе сохраняем корень и последний сегмент,
середину схлопываем в `…`; совсем узко → `…<sep><last>`; хвост длиннее max —
обрезаем хвост."""

from __future__ import annotations


def path_sep(path: str) -> str:
    return "\\" if "\\" in path else "/"


def truncate_path(path: str, max_cols: int) -> str:
    if max_cols <= 0:
        return ""
    if len(path) <= max_cols:
        return path
    sep = path_sep(path)
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
