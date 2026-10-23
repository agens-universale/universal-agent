"""Правки блока ленты из журнала правок (спека P4 §6, §9.9, §9.10а).

Единственный билдер для живой ленты и превью `/sessions`: запись журнала →
`BlockEdit` модели блоков (`gc_tui.services.tool_blocks`) и `DiffBlock` под
блоком. Нет журнала или записи — правки нет: `Edited путь` без счётчиков и
диффа решает модель.
"""

from __future__ import annotations

from typing import Optional

from gc_core.agent_edits.journal import EditRecord

from gc_tui.services.diff_render import copyable_hunks, diff_line_count
from gc_tui.services.edits_lookup import EditsLookup
from gc_tui.services.tool_blocks import (
    BlockEdit,
    DiffColumn,
    command_file_verb,
    edit_verb,
)
from gc_tui.widgets.diff_block import DiffBlock


def _edit_of(record: EditRecord, *, key: str, verb: str) -> BlockEdit:
    lines = diff_line_count(record.hunks)
    return BlockEdit(
        key=key,
        path=record.path,
        verb=verb,
        add=record.add,
        rem=record.del_,
        diff_lines=lines,
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
    """`DiffBlock` файла под блоком: доля ``rows``, колонка ``column``."""
    return DiffBlock.for_record(
        record,
        event_no=event_no,
        collapsed_max_lines=rows,
        compact=compact,
        in_block=True,
        column=column,
    )
