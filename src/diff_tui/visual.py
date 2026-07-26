"""Visual unified diff + truncation для Diff-TUI (не tool-output return_diff)."""
from __future__ import annotations

import difflib

from config_modules.diff_tui import (
    DEFAULT_FULL_FILE_MAX_LINES,
    DEFAULT_MAX_DIFF_CHARS,
    DEFAULT_PREVIEW_CONTEXT_LINES,
)

VISUAL_MODES = ("adaptive", "full")

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


def build_visual_diff(
    old: str,
    new: str,
    *,
    mode: str = "adaptive",
    full_file_max_lines: int = DEFAULT_FULL_FILE_MAX_LINES,
    preview_context_lines: int = DEFAULT_PREVIEW_CONTEXT_LINES,
    max_diff_chars: int = DEFAULT_MAX_DIFF_CHARS,
    fromfile: str = "a",
    tofile: str = "b",
) -> str:
    """Единый visual-diff builder для Enter (adaptive) и ``f`` (full).

    Спека §5, канон D1–D5:

    * ``adaptive``: если ``max(len(old), len(new))`` (по числу строк)
      ``<= full_file_max_lines`` → ``n`` без порога (весь файл);
      иначе ``n = preview_context_lines`` (preview со склейкой контекстов
      через ``difflib``).
    * ``full``: ``n`` без порога (весь файл) вне зависимости от размера.
    * ``truncate_diff`` применяется ко всему результату (D5).
    """
    if mode not in VISUAL_MODES:
        raise ValueError(
            f"unknown visual diff mode: {mode!r}; expected one of {VISUAL_MODES}"
        )

    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)

    if mode == "full":
        n = None  # без порога — весь файл
    else:
        # adaptive: короткий файл → весь, длинный → preview
        if max(len(old_lines), len(new_lines)) <= int(full_file_max_lines):
            n = None
        else:
            n = max(0, int(preview_context_lines))

    # ``difflib.unified_diff`` с n=None → склеивает пересекающиеся контексты
    # сам (hunk merge не пишем, D4).
    if n is None:
        # Очень большое n: все строки попадают в контекст каждого hunk.
        ctx = max(len(old_lines), len(new_lines))
    else:
        ctx = n

    text = "".join(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=fromfile,
            tofile=tofile,
            n=ctx,
        )
    )
    return truncate_diff(text, max_diff_chars)
