"""Строки статуса стартовой шапки: загрузка MCP, итог, сбой загрузки.

Чистые форматтеры: состояние берут у `McpPeephole`, кадр анимации — у
вызывающего. Цвета — только из палитры.
"""

from __future__ import annotations

from rich.text import Text

from gc_tui.palette import (
    ACCENT_BREATH_SHADES,
    DIM,
    ERR,
    INK,
    MCP_SHIMMER_SHADES,
    MUTED,
    NOTE_INK,
    OK,
    WARN,
)
from gc_tui.services.column_row import ColumnRow
from gc_tui.services.mcp_peephole import McpPeephole
from gc_tui.services.shimmer import shimmer_pass, shimmer_text

_LOADING_WORD = "подключаю MCP"


def loading_line(
    connected: int,
    total: int,
    shown: tuple[str, bool] | None,
    frame: int,
) -> Text:
    """`• подключаю MCP 3/9 · имя ✓`: точка дышит, слово с бликом."""
    glyph = ACCENT_BREATH_SHADES[frame % len(ACCENT_BREATH_SHADES)]
    step = frame % shimmer_pass(_LOADING_WORD, MCP_SHIMMER_SHADES)
    line = Text(no_wrap=True, overflow="ellipsis")
    line.append("•", style=glyph)
    line.append(" ")
    line.append_text(shimmer_text(_LOADING_WORD, step, MCP_SHIMMER_SHADES))
    line.append(f" {connected}/{total}", style=MUTED)
    if shown is not None:
        name, ok = shown
        line.append(" · ", style=MUTED)
        line.append(name, style=NOTE_INK)
        line.append(" ✓" if ok else " ✗", style=OK if ok else ERR)
    return line


def settled_row(connected: int, total: int) -> ColumnRow:
    """`✓ готов к работе` и, если MCP есть, хвост `MCP c/t · /mcp`."""
    atoms = [Text("готов к работе", style=INK)]
    if total > 0:
        label = f" MCP {connected}/{total} · /mcp"
        if connected < total:
            atoms.append(Text.assemble("   ", ("⚠", WARN), (label, DIM)))
        else:
            atoms.append(Text(f"  {label}", style=DIM))
    return ColumnRow(Text("✓ ", style=OK), atoms)


def failed_row(total: int) -> ColumnRow:
    """Сбой сборки: при MCP в конфиге — про загрузку MCP, без MCP — про
    сам агент (слово MCP и `/mcp` там сбили бы с толку)."""
    head = Text.assemble(("⚠", WARN), " ")
    label = (
        "MCP: загрузка не завершилась · /mcp"
        if total > 0
        else "агент не готов — причина в ленте ниже"
    )
    atom = Text(label, style=DIM)
    return ColumnRow(head, [atom])


def status_renderable(peephole: McpPeephole, frame: int) -> Text | ColumnRow:
    if peephole.phase == "loading":
        return loading_line(
            peephole.connected, peephole.total, peephole.shown, frame
        )
    if peephole.phase == "settled":
        return settled_row(peephole.connected, peephole.total)
    return failed_row(peephole.total)
