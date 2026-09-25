"""Rich-билдеры чек-листов session_todo — канон spec 2026-07-31 (v3).

Единственный владелец «как чек-лист выглядит»: основной трейс (§2) и
развёрнутый субагентский (§3) собираются здесь над общей основой (§1).
Цвета — только palette; выполненное = strike + $muted.

Чек-лист рендерится ПОСТРОЧНО (список ``TodoLine``): каждая строка — своя
пара «prefix (отступ + глиф) / content (текст)» для TraceRow, чтобы перенос
длинного пункта выравнивался под началом его текста (hanging indent — v4,
живая доводка 2026-08-01; раньше весь блок был одним Text, и перенос уходил
к уровню заголовка). Паритет plain-вида с ``_todo_checklist_summary`` (то, что уходит в
``TraceEvent.summary`` для копирования/поиска) пинится тестом.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.style import Style
from rich.text import Text

from gc_tui.palette import INK, MUTED, TOOL_NAME

_GLYPHS = {"completed": "✓", "in_progress": "→", "pending": "○"}

# (стиль глифа, стиль текста) per статус: [0] — основной блок, [1] — субагентский.
# Глиф — в цвете текста своего пункта (без strike): статус кодирует форма
# значка, цветовые акценты остаются у индикаторов тулов (доводка 2026-08-01;
# было ✓ $ok / → $run-color). Субагент: живая часть (заголовок, →, ○) —
# $tool-name, в тон именам тулов его блока; выполненное гаснет до $muted.
_PALETTES = (
    {  # основной: блок живёт в $ink, выполненное гаснет до $muted
        "completed": (Style(color=MUTED), Style(color=MUTED, strike=True)),
        "in_progress": (Style(color=INK), Style(color=INK)),
        "pending": (Style(color=INK), Style(color=INK)),
    },
    {  # субагент
        "completed": (Style(color=MUTED), Style(color=MUTED, strike=True)),
        "in_progress": (Style(color=TOOL_NAME), Style(color=TOOL_NAME)),
        "pending": (Style(color=TOOL_NAME), Style(color=TOOL_NAME)),
    },
)

_HEADER_STYLES = (Style(color=INK), Style(color=TOOL_NAME))

_ITEM_INDENT = "  "  # пункты на +2 от заголовка (оба чек-листа, spec v2)

# CSS-классы строк чек-листа: воздух сверху у заголовка, у субагентского —
# ещё и снизу (последняя строка блока), см. styles.tcss.
LINE_TODO_CLASS = "line-todo"
LINE_TODO_NESTED_CLASS = "line-todo-nested"
LINE_TODO_ITEM_CLASS = "line-todo-item"
# Классы блока-контейнера: воздух вокруг чек-листа и подсветка фокуса.
TODO_BLOCK_CLASS = "todo-block"
TODO_BLOCK_NESTED_CLASS = "todo-block-nested"


@dataclass(frozen=True)
class TodoLine:
    """Одна строка чек-листа: prefix → prefix-колонка TraceRow, content → text."""

    prefix: Text
    content: Text


def _build_lines(
    items: list, *, header_label: str, indent: str, subagent: bool
) -> list[TodoLine]:
    rows = [
        i
        for i in items or []
        if isinstance(i, dict)
        and str(i.get("content") or "").strip()
        and i.get("status") in _GLYPHS
    ]
    done = sum(1 for i in rows if i["status"] == "completed")
    header_style = _HEADER_STYLES[1 if subagent else 0]
    if not rows:
        return [
            TodoLine(
                prefix=Text(indent),
                content=Text(
                    f"session_todo   {header_label} очищен", style=header_style
                ),
            )
        ]
    lines = [
        TodoLine(
            prefix=Text(indent),
            content=Text(
                f"session_todo   {header_label} {done}/{len(rows)}",
                style=header_style,
            ),
        )
    ]
    styles = _PALETTES[1 if subagent else 0]
    for item in rows:
        glyph_style, item_style = styles[item["status"]]
        prefix = Text(f"{indent}{_ITEM_INDENT}")
        prefix.append(_GLYPHS[item["status"]], style=glyph_style)
        prefix.append(" ")
        lines.append(
            TodoLine(
                prefix=prefix,
                content=Text(str(item["content"]).strip(), style=item_style),
            )
        )
    return lines


def build_main_todo_lines(items: list, *, indent: str = "") -> list[TodoLine]:
    """Основной чек-лист (§2) построчно; indent — базовый отступ трейса."""
    return _build_lines(
        items, header_label="чек-лист", indent=indent, subagent=False
    )


def build_subagent_todo_lines(
    items: list, *, indent: str = ""
) -> list[TodoLine]:
    """Чек-лист субагента (§3) построчно; indent — nested-отступ блока."""
    return _build_lines(
        items, header_label="чек-лист субагента", indent=indent, subagent=True
    )


def build_todo_rows(
    items: list, *, subagent: bool, indent: str = "", event_no: int = 0
) -> list:
    """Строки чек-листа как ``TraceRow`` — внутренности блока (см. build_todo_block).

    Строки не фокусируемы: выделение живёт на блоке целиком, иначе чек-лист из
    N пунктов дал бы N Tab-стопов вместо одного на событие. ``event_no`` несут
    все строки — Ctrl+клик открывает payload с любой из них.
    """
    from gc_tui.widgets.trace_row import TraceRow

    builder = build_subagent_todo_lines if subagent else build_main_todo_lines
    lines = builder(items or [], indent=indent)
    header_class = LINE_TODO_NESTED_CLASS if subagent else LINE_TODO_CLASS
    return [
        TraceRow(
            prefix=line.prefix,
            content=line.content,
            kind_class=header_class if i == 0 else LINE_TODO_ITEM_CLASS,
            event_no=event_no,
            focusable=False,
        )
        for i, line in enumerate(lines)
    ]


def build_todo_block(
    items: list, *, subagent: bool, indent: str = "", event_no: int = 0
):
    """Готовый виджет чек-листа для монтирования — общий для всех каналов.

    Каналы (живой основной, read-only основной, живой субагентский,
    sidecar/превью) различаются ТОЛЬКО способом монтирования и монтируют ОДИН
    виджет: клик по любой строке выделяет весь блок, как у прозы.
    """
    from gc_tui.widgets.todo_block import TodoBlock

    rows = build_todo_rows(
        items, subagent=subagent, indent=indent, event_no=event_no
    )
    classes = TODO_BLOCK_NESTED_CLASS if subagent else TODO_BLOCK_CLASS
    return TodoBlock(rows, event_no=event_no, classes=classes)
