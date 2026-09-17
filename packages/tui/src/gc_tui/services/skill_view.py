"""Презентация скилла для TUI: RU-метки типа, усечение пути, контент вьювера.

Владелец ответственности «как показать скилл» — этот модуль; SkillsScreen
использует его (source_label / truncate_path_left / build_skill_viewer),
не дублируя форматирование.
"""
from __future__ import annotations

SOURCE_LABELS = {
    "system": "системный",
    "user": "пользовательский",
    "project": "проектный",
}


def source_label(source: str) -> str:
    """RU-ярлык типа скилла; неизвестный source возвращается как есть."""
    return SOURCE_LABELS.get(source, source or "?")


def truncate_path_left(path: str, maxlen: int) -> str:
    """Усечь путь слева с ведущим «…», сохранив хвост (папка скилла важнее начала).

    Контракт: maxlen ≥ 2. Короткий путь возвращается без изменений.
    """
    if len(path) <= maxlen:
        return path
    return "…" + path[-(maxlen - 1):]


def build_skill_viewer(detail: dict) -> tuple[str, str]:
    """(title, body) для ViewerScreen скилла.

    title: `skill · <id> · <тип> · <root_path>`.
    body: markdown — заголовок, описание, разделитель, инструкции.
    """
    sid = detail.get("id") or "?"
    title = (
        f"skill · {sid} · {source_label(detail.get('source', ''))}"
        f" · {detail.get('root_path', '')}"
    )
    body = "\n".join([
        f"# {detail.get('name') or sid} ({sid})",
        "",
        detail.get("description", "") or "(без описания)",
        "",
        "---",
        "",
        detail.get("instructions", "") or "(нет инструкций)",
    ])
    return title, body
