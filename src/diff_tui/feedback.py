"""Technical user message после undo в Diff-TUI (для agent thread).

UX-строки для DiffScreen / trace — отдельно от technical message агенту.
"""
from __future__ import annotations

from typing import Sequence

from message_policy.user_message_policy import (
    TECHNICAL_TYPE_TECHNICAL,
    wrap_technical_user_content,
)
from utils.generate_message_id import generate_msg_id_sha256 as generate_message_id_func


def files_word(n: int) -> str:
    """Русское склонение «файл/файла/файлов»."""
    n_abs = abs(n) % 100
    n1 = n_abs % 10
    if 11 <= n_abs <= 19:
        return "файлов"
    if n1 == 1:
        return "файл"
    if 2 <= n1 <= 4:
        return "файла"
    return "файлов"


def format_undo_ack(undone_keys: Sequence[str]) -> str | None:
    """Короткий ack для DiffScreen / trace после undo."""
    keys = [k for k in undone_keys if k]
    if not keys:
        return None
    if len(keys) == 1:
        return f"Отменено: {keys[0]}"
    return f"Отменено {len(keys)} {files_word(len(keys))}"


def format_undo_noop_ack(key: str, reason: str) -> str | None:
    """Ack когда undo не применился (created / untracked)."""
    if not key:
        return None
    if reason == "created_no_approve":
        return f"{key}: отмена невозможна — удалите файл вручную"
    if reason == "untracked":
        return f"{key}: отмена невозможна — удалите файл вручную"
    return None


def format_approve_ack(count: int, key: str | None = None) -> str | None:
    """Короткий ack после approve (одного или all)."""
    if count <= 0:
        return None
    if count == 1 and key:
        return f"Принято: {key}"
    return f"Принято {count} {files_word(count)}"


def format_pending_cta(count: int) -> str | None:
    """Post-turn CTA в трейсе, если есть pending."""
    if count <= 0:
        return None
    return f"Изменено {count} {files_word(count)}. /diff для просмотра"


def build_undo_feedback_body(undone_keys: Sequence[str]) -> str:
    lines = "\n".join(f"- {k}" for k in undone_keys)
    return (
        "User reverted pending file changes via Diff-UI undo.\n"
        f"Files restored to the previous on-disk baseline:\n{lines}\n"
        "Re-read these files from disk before assuming their contents."
    )


def build_undo_feedback_message(undone_keys: Sequence[str]) -> dict | None:
    """``role:user`` technical message или ``None``, если ключей нет."""
    keys = [k for k in undone_keys if k]
    if not keys:
        return None
    return {
        "role": "user",
        "content": wrap_technical_user_content(
            build_undo_feedback_body(keys),
            technical_type=TECHNICAL_TYPE_TECHNICAL,
        ),
        "id": generate_message_id_func("m_diff_undo"),
    }
