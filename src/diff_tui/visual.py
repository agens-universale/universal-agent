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
