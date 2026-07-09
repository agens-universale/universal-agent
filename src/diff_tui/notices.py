"""Тексты runtime-notice для no-op undo Diff-TUI."""
from __future__ import annotations

from startup_notices import Notice


NOTICE_UNDO_CREATED = (
    "файл не откатывается; удалите вручную, если не нужен"
)
NOTICE_UNDO_UNTRACKED = (
    "undo не применяется к untracked; удалите вручную или добавьте в .gitignore"
)


def notice_undo_created(path_key: str) -> Notice:
    return Notice(
        level="info",
        code="diff_tui_undo_created",
        message=f"{path_key}: {NOTICE_UNDO_CREATED}",
    )


def notice_undo_untracked(path_key: str) -> Notice:
    return Notice(
        level="info",
        code="diff_tui_undo_untracked",
        message=f"{path_key}: {NOTICE_UNDO_UNTRACKED}",
    )
