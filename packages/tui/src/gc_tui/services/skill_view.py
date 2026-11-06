"""Презентация скилла для TUI: RU-метки типа, усечение пути, контент вьювера,
запускаемое имя.

Владелец ответственности «как показать скилл» — этот модуль; SkillsScreen
использует его (source_label / truncate_path_left / build_skill_viewer),
палитра и реплика — skill_input_name, не дублируя форматирование. Команды
плагинов палитра показывает строкой скилла (`plugin_command_rows`): имена
команд и скиллов — одно пространство (§4.2).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from gc_core.plugins.commands import resolve_slash_name
from gc_core.plugins.manifest import PluginCommand
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
    body: markdown — заголовок, описание, инструкции (без `---`: движок
    ленты линию не рисует, а сырой знак в просмотре — мусор).
    """
    sid = detail.get("id") or "?"
    title = f"skill · {sid} · {source_label(detail.get('source', ''))} · {detail.get('root_path', '')}"
    body = "\n".join(
        [
            f"# {detail.get('name') or sid} ({sid})",
            "",
            detail.get("description", "") or "(без описания)",
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
    commands: Sequence[tuple[str, PluginCommand]] = (),
) -> str:
    """Запускаемый вид скилла (без `/`) — как его вставляют Enter/Tab
    палитры и как он показан в реплике: короткое имя, если оно ведёт ровно
    к этому скиллу и не занято командой, алиасом или командой плагина
    (`commands` снимка), иначе полный id. Каталога нет (холодный кэш,
    превью) — короткое имя."""
    short = skill_id.rpartition(":")[2]
    if is_command is not None and is_command(short):
        return skill_id
    if any(cmd.name == short for _, cmd in commands):
        return skill_id
    if catalog is None:
        return short
    res = resolve_skill_exact(short, catalog)
    if isinstance(res, Resolved) and res.canonical_id == skill_id:
        return short
    return skill_id


def plugin_command_rows(
    commands: Sequence[tuple[str, PluginCommand]],
    skill_ids: Sequence[str],
    *,
    is_command: Callable[[str], bool],
) -> list[dict]:
    """Строки палитры для команд плагинов — записи вида скилла с пометкой
    `plugin`. Имя короткое, если не занято командой или алиасом и функция
    ядра ведёт по нему ровно к этой команде, иначе `<плагин>:<команда>`."""
    rows: list[dict] = []
    for plugin, cmd in commands:
        full = f"{plugin}:{cmd.name}"
        found = resolve_slash_name(cmd.name, commands, skill_ids)
        short = not is_command(cmd.name) and found.canonical == full
        rows.append(
            {
                "id": full,
                "description": cmd.description,
                "input_name": cmd.name if short else full,
                "plugin": plugin,
            }
        )
    return rows
