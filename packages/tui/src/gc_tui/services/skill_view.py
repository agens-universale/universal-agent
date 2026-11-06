"""Презентация скилла для TUI: RU-метки типа, усечение пути, контент вьювера,
запускаемое имя.

Владелец ответственности «как показать скилл» — этот модуль; SkillsScreen
использует его (source_label / truncate_path_left / build_skill_viewer),
палитра и реплика — skill_input_name, не дублируя форматирование.
"""

from __future__ import annotations

from collections.abc import Callable

from gc_core.tools.skills.skill_arg_resolver import (
    Resolved,
    SkillCatalog,
    resolve_skill_exact,
)

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
    return "…" + path[-(maxlen - 1) :]


def build_skill_viewer(detail: dict) -> tuple[str, str]:
    """(title, body) для ViewerScreen скилла.

    title: `skill · <id> · <тип> · <root_path>`.
    body: markdown — заголовок, описание, разделитель, инструкции.
    """
    sid = detail.get("id") or "?"
    title = f"skill · {sid} · {source_label(detail.get('source', ''))} · {detail.get('root_path', '')}"
    body = "\n".join(
        [
            f"# {detail.get('name') or sid} ({sid})",
            "",
            detail.get("description", "") or "(без описания)",
            "",
            "---",
            "",
            detail.get("instructions", "") or "(нет инструкций)",
        ]
    )
    return title, body


def skill_input_name(
    skill_id: str,
    catalog: SkillCatalog | None = None,
    *,
    is_command: Callable[[str], bool] | None = None,
) -> str:
    """Запускаемый вид скилла (без `/`) — как его вставляют Enter/Tab
    палитры и как он показан в реплике: короткое имя, если оно ведёт ровно
    к этому скиллу и не занято командой или алиасом, иначе полный id.
    Каталога нет (холодный кэш, превью) — короткое имя."""
    short = skill_id.rpartition(":")[2]
    if is_command is not None and is_command(short):
        return skill_id
    if catalog is None:
        return short
    res = resolve_skill_exact(short, catalog)
    if isinstance(res, Resolved) and res.canonical_id == skill_id:
        return short
    return skill_id
