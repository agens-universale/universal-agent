"""Visual unified diff + truncation для Diff-TUI (не tool-output return_diff)."""
from __future__ import annotations

import difflib

from config_modules.diff_tui import (
    DEFAULT_MAX_DIFF_CHARS,
    DEFAULT_PREVIEW_CONTEXT_LINES,
)

_TRUNCATION_MARKER = "\n... [diff truncated]\n"


def truncate_diff(text: str, max_chars: int = DEFAULT_MAX_DIFF_CHARS) -> str:
    """Усечь visual diff по ``max_diff_chars`` (0 = без лимита)."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + _TRUNCATION_MARKER


def build_unified_diff(
    old: str,
    new: str,
    *,
    fromfile: str = "a",
    tofile: str = "b",
    context_lines: int = DEFAULT_PREVIEW_CONTEXT_LINES,
    max_diff_chars: int = DEFAULT_MAX_DIFF_CHARS,
) -> str:
    """``difflib.unified_diff`` + truncation из ``diff_tui.*``."""
    n = max(0, int(context_lines))
    text = "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=fromfile,
            tofile=tofile,
            n=n,
        )
    )
    return truncate_diff(text, max_diff_chars)


def build_created_file_diff(
    content: str,
    *,
    tofile: str,
    context_lines: int = DEFAULT_PREVIEW_CONTEXT_LINES,
    max_diff_chars: int = DEFAULT_MAX_DIFF_CHARS,
) -> str:
    """Unified diff ``/dev/null`` → новый файл.

    ``difflib`` для пустого файла даёт пустую строку — тогда всё равно
    возвращаем заголовок addition, чтобы viewer/preview не были пустыми.
    """
    text = build_unified_diff(
        "",
        content,
        fromfile="/dev/null",
        tofile=tofile,
        context_lines=context_lines,
        max_diff_chars=max_diff_chars,
    )
    if text.strip():
        return text
    return truncate_diff(
        f"--- /dev/null\n+++ {tofile}\n@@ -0,0 +0,0 @@\n",
        max_diff_chars,
    )


def preview_change_lines(diff_text: str, *, limit: int = 3) -> tuple[list[str], int]:
    """Первые ``limit`` строк изменения для trace-preview.

    Пропускаем ``---`` / ``+++`` / ``@@``: для created они занимают весь
    3-строчный preview и содержимое файла не видно.
    """
    if not diff_text or not diff_text.strip():
        return [], 0
    all_lines = [ln for ln in diff_text.splitlines() if ln != ""]
    change_lines = [
        ln
        for ln in all_lines
        if ln.startswith(("+", "-"))
        and not ln.startswith(("+++", "---"))
    ]
    chosen = change_lines if change_lines else all_lines
    head = chosen[: max(0, int(limit))]
    return head, max(0, len(chosen) - len(head))
