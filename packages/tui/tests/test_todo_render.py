"""Билдеры rich.Text чек-листов — канон spec 2026-07-31 §1-§3."""

from gc_tui import palette
from gc_tui.services.todo_render import (
    build_main_todo_lines,
    build_subagent_todo_lines,
)
from gc_tui.services.trace_mapper import _todo_checklist_summary
from rich.text import Text

ITEMS = [
    {"content": "Изучить структуру", "status": "completed"},
    {"content": "Написать тесты", "status": "in_progress"},
    {"content": "Прогнать приёмку", "status": "pending"},
]


def _text(lines) -> Text:
    """Строки чек-листа → один Text: удобно проверять plain-вид и зоны стилей."""
    text = Text()
    for i, line in enumerate(lines):
        if i:
            text.append("\n")
        text.append_text(line.prefix)
        text.append_text(line.content)
    return text


def _spans_by_text(text: Text) -> dict[str, str]:
    """{фрагмент: style-строка} для проверки зон."""
    plain = text.plain
    return {plain[s.start : s.end]: str(s.style) for s in text.spans}


def test_main_header_counts_completed_of_total():
    text = _text(build_main_todo_lines(ITEMS))
    assert text.plain.splitlines()[0] == "session_todo   чек-лист 1/3"


def test_main_lines_glyphs_and_indent():
    lines = _text(build_main_todo_lines(ITEMS)).plain.splitlines()
    assert lines[1] == "  ✓ Изучить структуру"
    assert lines[2] == "  → Написать тесты"
    assert lines[3] == "  ○ Прогнать приёмку"


def test_main_completed_struck_and_dim():
    spans = _spans_by_text(_text(build_main_todo_lines(ITEMS)))
    done_style = spans["Изучить структуру"]
    assert "strike" in done_style and palette.MUTED.lower() in done_style
    glyph_done = spans["✓"]
    # Глиф — в цвете текста пункта, но без strike (доводка 2026-08-01).
    assert palette.MUTED.lower() in glyph_done and "strike" not in glyph_done


def test_main_in_progress_and_pending_ink():
    spans = _spans_by_text(_text(build_main_todo_lines(ITEMS)))
    assert palette.INK.lower() in spans["→"]
    assert palette.INK.lower() in spans["Написать тесты"]
    assert "strike" not in spans["Написать тесты"]


def test_main_empty_is_cleared_line():
    text = _text(build_main_todo_lines([]))
    assert text.plain == "session_todo   чек-лист очищен"


def test_main_builder_plain_matches_summary_parity():
    assert (
        _text(build_main_todo_lines(ITEMS)).plain
        == f"session_todo   {_todo_checklist_summary(ITEMS)}"
    )
    assert (
        _text(build_main_todo_lines([])).plain
        == f"session_todo   {_todo_checklist_summary([])}"
    )


def test_subagent_empty_is_cleared_line():
    # Достижимо с v3 (каждый вызов развёрнут): очистка тоже идёт через билдер.
    assert (
        _text(build_subagent_todo_lines([])).plain
        == "session_todo   чек-лист субагента очищен"
    )


def test_subagent_header_and_indent():
    lines = _text(build_subagent_todo_lines(ITEMS)).plain.splitlines()
    assert lines[0] == "session_todo   чек-лист субагента 1/3"
    assert lines[1] == "  ✓ Изучить структуру"


def test_subagent_completed_struck_muted_open_tool_name():
    spans = _spans_by_text(_text(build_subagent_todo_lines(ITEMS)))
    done_style = spans["Изучить структуру"]
    assert "strike" in done_style and palette.MUTED.lower() in done_style
    # Живая часть (→/○ и заголовок) — в тон именам тулов блока (доводка 2026-08-01).
    assert palette.TOOL_NAME.lower() in spans["Написать тесты"]
    assert (
        palette.TOOL_NAME.lower()
        in spans["session_todo   чек-лист субагента 1/3"]
    )


# --- общая обвязка строк для всех каналов -----------------------------------


def test_build_todo_rows_classes_per_channel():
    """Заголовок отличается классом по каналу, пункты — общим line-todo-item."""
    import pytest

    pytest.importorskip("textual")
    from gc_tui.services.todo_render import build_todo_rows

    main = build_todo_rows(ITEMS, subagent=False, indent="   ", event_no=7)
    assert len(main) == 4  # заголовок + три пункта
    assert main[0].has_class("line-todo")
    assert main[1].has_class("line-todo-item")
    assert all(r._event_no == 7 for r in main)

    sub = build_todo_rows(ITEMS, subagent=True, indent="       ")
    assert sub[0].has_class("line-todo-nested")


# --- блок как единица выделения ---------------------------------------------


def test_todo_block_is_single_focusable_widget():
    """Клик по любой строке подсвечивает весь чек-лист: блок — один виджет.

    Строки внутри не фокусируемы, Textual поднимается до фокусируемого предка
    (Screen.get_focusable_widget_at), поэтому Tab-стоп остаётся один на событие.
    """
    import pytest

    pytest.importorskip("textual")
    from gc_tui.services.todo_render import build_todo_block
    from gc_tui.widgets.trace_row import TraceRow

    block = build_todo_block(ITEMS, subagent=False, indent="   ", event_no=7)
    assert block.can_focus
    rows = block.todo_rows
    assert len(rows) == 4  # заголовок + три пункта
    assert all(isinstance(r, TraceRow) for r in rows)
    assert not any(r.can_focus for r in rows)
    # Ctrl+клик с любой строки открывает payload события.
    assert all(r._event_no == 7 for r in rows)


def test_todo_block_classes_by_channel():
    import pytest

    pytest.importorskip("textual")
    from gc_tui.services.todo_render import build_todo_block

    main = build_todo_block(ITEMS, subagent=False)
    sub = build_todo_block(ITEMS, subagent=True)
    assert main.has_class("todo-block") and not main.has_class(
        "todo-block-nested"
    )
    assert sub.has_class("todo-block-nested")
