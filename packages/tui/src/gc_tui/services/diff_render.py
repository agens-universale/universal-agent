"""Подсветка unified diff для TUI (viewer /diff).

`+` → OK (зелёный), `-` → ERR (красный), заголовки/@@ → MUTED,
контекст и прочее — CODE_BLOCK (как раньше монохромный дифф).
"""
from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from gc_tui.palette import CODE_BLOCK, ERR, MUTED, OK


def style_unified_diff(text: str) -> Text:
    """Раскрасить unified diff построчно в Rich Text."""
    out = Text()
    if not text:
        return out
    for line in text.splitlines(keepends=True):
        if line.endswith("\n"):
            content, nl = line[:-1], "\n"
        else:
            content, nl = line, ""
        out.append(content + nl, style=_line_style(content))
    return out


def render_unified_diff_widget(text: str) -> Static:
    """Static с раскрашенным unified diff (для DiffViewerScreen)."""
    return Static(style_unified_diff(text), markup=False)


def _line_style(line: str) -> str:
    if line.startswith("---") or line.startswith("+++"):
        return MUTED
    if line.startswith("@@"):
        return MUTED
    if line.startswith("\\"):  # \ No newline at end of file
        return MUTED
    if line.startswith("+"):
        return OK
    if line.startswith("-"):
        return ERR
    return CODE_BLOCK
