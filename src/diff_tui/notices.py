"""Тексты runtime-notice для no-op undo Diff-TUI."""
from __future__ import annotations

from startup_notices import Notice


NOTICE_UNDO_CREATED = (
    "отмена невозможна — удалите файл вручную"
)
NOTICE_UNDO_UNTRACKED = (
    "отмена невозможна для untracked — удалите вручную или добавьте в .gitignore"
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
