"""Запись удаления по байтам «до» и бюджет строк удалений на команду.

Общая для обоих подканалов шелл-делты (карманного и git): форма записи одна,
поэтому владелец отдельный, а не ``shell_delta`` (тот и так велик). Модуль не
импортирует ``shell_delta`` — иначе цикл.
"""

from __future__ import annotations

import time

from gc_core.agent_edits.deltas import (
    build_diff,
    decode_edit_text,
    truncate_hunks,
)
from gc_core.agent_edits.journal import EditRecord
from gc_core.utils.file_text import (
    BINARY_SAMPLE_BYTES,
    classify_content_from_sample,
    sha256_hex_of_bytes,
)


def is_binary_bytes(data: bytes) -> bool:
    """Тот же классификатор, что у рекордера, но по байтам уже в памяти
    (второго чтения файла не нужно)."""
    return classify_content_from_sample(
        "", data[:BINARY_SAMPLE_BYTES]
    ).is_binary


class DeleteBudget:
    """Остаток байт, из которых команда может нести строки удалений."""

    def __init__(self, limit: int) -> None:
        self._left = limit

    def take(self, size: int) -> bool:
        """``True`` и вычитает, если ``size`` влезает; иначе ``False``."""
        if size > self._left:
            return False
        self._left -= size
        return True


def _deleted_text(
    before: bytes | None, budget: DeleteBudget | None
) -> str | None:
    """Текст «до», если строки удаления положены; бюджет тратится последним."""
    if before is None or is_binary_bytes(before):
        return None
    text = decode_edit_text(before)
    if text is None:
        return None
    if budget is not None and not budget.take(len(before)):
        return None
    return text


def delete_record(
    key: str,
    turn_id: str,
    before: bytes | None,
    *,
    mode: int | None,
    budget: DeleteBudget | None,
    context_lines: int,
    max_chars: int,
) -> EditRecord:
    """Запись ``op="delete"`` шелл-канала (``call_id=None``).

    Строки удаления — у текстового файла с известным «до» в пределах бюджета;
    иначе ``no_diff``. ``budget=None`` — бюджет уже зарезервирован вызывающим.
    """
    text = _deleted_text(before, budget)
    hunks, add, deleted, flags = "", 0, 0, ["no_diff"]
    if text is not None:
        body, add, deleted = build_diff(text, "", context_lines)
        hunks, flags = truncate_hunks(
            f"diff --git a/{key} b/{key}\n{body}", max_chars
        )
    return EditRecord(
        path=key,
        turn_id=turn_id,
        ts=time.time(),
        call_id=None,
        op="delete",
        hunks=hunks,
        add=add,
        del_=deleted,
        before_sha256=(
            None if before is None else sha256_hex_of_bytes(before)
        ),
        after_sha256=None,
        flags=flags,
        mode=mode,
    )
