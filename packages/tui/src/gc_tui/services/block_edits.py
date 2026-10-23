"""Правки блока ленты из журнала правок (спека P4 §6, §9.9, §9.10а).

Единственный билдер для живой ленты и превью `/sessions`: запись журнала →
`BlockEdit` модели блоков (`gc_tui.services.tool_blocks`) и `DiffBlock` под
блоком. Нет журнала или записи — правки нет: `Edited путь` без счётчиков и
диффа решает модель.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from gc_core.agent_edits.ctx import section_limit
from gc_core.agent_edits.journal import EditRecord
from gc_core.config_modules.agent_edits import (
    DEFAULT_REPLAY_EXPANDED_TURNS,
    DEFAULT_TRACE_INLINE_MAX_LINES,
)

from gc_tui.models import CommandOutput
from gc_tui.services.diff_render import (
    copyable_hunks,
    diff_changed_count,
    diff_line_count,
)
from gc_tui.services.edits_lookup import EditsLookup
from gc_tui.services.tool_blocks import (
    BlockEdit,
    DiffColumn,
    command_file_verb,
    edit_verb,
)
from gc_tui.services.trace_retention import is_light_payload
from gc_tui.widgets.diff_block import DiffBlock


@dataclass(frozen=True)
class RanResult:
    """Что знает лента о законченной команде: разобранный вывод, правки
    команды из журнала, незаписанные файлы сверх потолка путей."""

    output: Optional[CommandOutput]
    files: Sequence[BlockEdit] = ()
    unrecorded: int = 0
    #: Запись вне окна retention: всего вывода при раскрытии нет.
    light: bool = False
    #: Ключи файлов с компактным диффом (ход вне раскрытых).
    compact: frozenset[str] = field(default_factory=frozenset)


def edit_settings(section: Mapping[str, Any]) -> tuple[int, int]:
    """Секция `agent_edits` → (изменённых строк свёрнутого превью диффа
    на файл, окно раскрытых ходов `replay_expanded_turns`). Окно `-1` — не
    режем (ровно `0` в конфиге); прочий мусор — дефолт, общим правилом
    ядра."""
    preview = section_limit(
        section, "trace_inline_max_lines", DEFAULT_TRACE_INLINE_MAX_LINES
    )
    raw = section.get("replay_expanded_turns", DEFAULT_REPLAY_EXPANDED_TURNS)
    if isinstance(raw, int) and not isinstance(raw, bool) and raw == 0:
        return preview, -1
    window = section_limit(
        section, "replay_expanded_turns", DEFAULT_REPLAY_EXPANDED_TURNS
    )
    return preview, window


def stale_turn(lookup: EditsLookup, turn_id: str, window: int) -> bool:
    """Ход вне окна последних ``window`` ходов журнала — дифф стартует
    компактным (§6.3); ``window < 0`` — окно не режем. Окно по журналу:
    правило одно для живой ленты, переоткрытия и превью."""
    if window < 0:
        return False
    return turn_id not in lookup.turn_ids()[-window:]


def _edit_of(record: EditRecord, *, key: str, verb: str) -> BlockEdit:
    lines = diff_line_count(record.hunks)
    return BlockEdit(
        key=key,
        path=record.path,
        verb=verb,
        add=record.add,
        rem=record.del_,
        diff_lines=lines,
        changed_lines=diff_changed_count(record.hunks),
        size_bytes=record.size_bytes,
        has_record=True,
        copy_text=copyable_hunks(record.hunks, path=record.path),
        record=record,
    )


def edit_of_call(
    lookup: Optional[EditsLookup], call_id: str
) -> Optional[BlockEdit]:
    """Правка fs-тула по его вызову; глагол — по `op` записи (§6.1)."""
    if lookup is None or not call_id:
        return None
    record = lookup.by_call_id(call_id)
    if record is None:
        return None
    verb = edit_verb(record.op, failed=False)
    return _edit_of(record, key=call_id, verb=verb)


def call_edit(
    lookup: Optional[EditsLookup], call_id: str, *, window: int
) -> tuple[Optional[BlockEdit], bool]:
    """Правка вызова и «ход вне окна раскрытых» (компактный дифф)."""
    edit = edit_of_call(lookup, call_id)
    if lookup is None or edit is None or edit.record is None:
        return edit, False
    return edit, stale_turn(lookup, edit.record.turn_id, window)


def ran_result_of(
    lookup: Optional[EditsLookup], ev: Any, *, window: int
) -> RanResult:
    """Законченная команда: вывод из события (разобран до облегчения
    retention) и правки команды из журнала по её `call_id`."""
    files, unrecorded = ran_files_of(lookup, ev.call_id or "")
    compact = frozenset(
        f.key
        for f in files
        if lookup is not None
        and f.record is not None
        and stale_turn(lookup, f.record.turn_id, window)
    )
    return RanResult(
        output=ev.command,
        files=files,
        unrecorded=unrecorded,
        light=is_light_payload(ev),
        compact=compact,
    )


def ran_files_of(
    lookup: Optional[EditsLookup], call_id: str
) -> tuple[list[BlockEdit], int]:
    """Файлы, тронутые командой ``call_id``, и число незаписанных сверх
    потолка путей (`command_paths`)."""
    if lookup is None or not call_id:
        return [], 0
    files = [
        _edit_of(
            record,
            key=f"{call_id}:{record.path}",
            verb=command_file_verb(record.op),
        )
        for record in lookup.by_command(call_id)
    ]
    total = lookup.command_paths_of(call_id)
    unrecorded = max(0, total - len(files)) if total else 0
    return files, unrecorded


def diff_block_for(
    record: EditRecord,
    *,
    rows: int,
    column: DiffColumn,
    compact: bool,
    event_no: int = 0,
) -> DiffBlock:
    """`DiffBlock` файла под блоком: превью ``rows`` изменённых строк,
    колонка ``column``."""
    return DiffBlock.for_record(
        record,
        event_no=event_no,
        collapsed_max_lines=rows,
        compact=compact,
        in_block=True,
        column=column,
    )
