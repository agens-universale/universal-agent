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
from gc_core.agent_edits.git_ops import quote_c_path
from gc_core.agent_edits.journal import EditRecord
from gc_core.utils.file_text import (
    BINARY_SAMPLE_BYTES,
    classify_content_from_sample,
    detect_unicode_bom,
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

    def fits(self, size: int) -> bool:
        """Влезает ли ``size`` в остаток — без резерва."""
        return size <= self._left

    def refund(self, size: int) -> None:
        """Вернуть зарезервированное, но не потраченное (бинарь, не текст):
        вернувшиеся байты достаются следующим резервам."""
        self._left += size


def deletion_text(before: bytes) -> str | None:
    """Текст «до» для строк удаления; бинарь или недекодируемый — ``None``.

    Декодер срезает ровно один BOM UTF-8, а ``git apply`` сверяет строки
    удаления с байтами файла: BOM возвращается в начало первой строки, иначе
    копия патча не применяется. Счётчики и показ он не меняет (нулевая
    ширина).
    """
    if is_binary_bytes(before):
        return None
    text = decode_edit_text(before)
    if text is not None and detect_unicode_bom(before) == "utf-8-sig":
        return "\ufeff" + text
    return text


def _deleted_text(
    before: bytes | None, budget: DeleteBudget | None
) -> str | None:
    """Текст «до», если строки удаления положены; бюджет тратится последним."""
    if before is None:
        return None
    text = deletion_text(before)
    if text is None:
        return None
    if budget is not None and not budget.take(len(before)):
        return None
    return text


def _git_delete_patch(key: str, mode: int | None, text: str, diff: str) -> str:
    """Удаление в форме ``git diff``: копия файла в UTF-8 применяется
    ``git apply`` (строки — декодированный текст: cp1251, UTF-16 — нет).

    Пустые заголовки ``--- ``/``+++ `` от ``build_diff`` — первые две строки
    потока (см. его докстринг) — заменяются именами git; у пустого файла
    хэнков нет, и, как у git, нет и строк ``---``/``+++``. ``build_diff``
    дописывает перевод строки последней строке без него — без маркера
    ``\\ No newline at end of file`` такой патч ``git apply`` не принимает.
    Имя с табуляцией, переводом строки, ``"`` или ``\\`` — в C-кавычках,
    как у git (``git_ops.quote_c_path``).
    """
    file_mode = "100755" if mode is not None and mode & 0o111 else "100644"
    old, new = quote_c_path(f"a/{key}"), quote_c_path(f"b/{key}")
    header = f"diff --git {old} {new}\ndeleted file mode {file_mode}\n"
    body = diff.split("\n", 2)[2] if diff else ""
    if not body:
        return header
    if not text.endswith("\n"):
        body += "\\ No newline at end of file\n"
    return f"{header}--- {old}\n+++ /dev/null\n{body}"


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
        diff, add, deleted = build_diff(text, "", context_lines)
        hunks, flags = truncate_hunks(
            _git_delete_patch(key, mode, text, diff), max_chars
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
