"""trace_render — общие строители виджетов трейса (Spec B / A+)."""

import pytest
from gc_testkit.tui_support import tui_settings


def test_compaction_label_with_chars():
    comp = {
        "messages_in_segment": 8,
        "chars_before": 12300,
        "chars_after": 3100,
        "synthesis": "x",
    }
    from gc_tui.services.trace_render import build_compaction_label

    assert (
        build_compaction_label(1, comp)
        == "контекст сжат #1 · 8→1 · 12.3k→3.1k симв"
    )


def test_compaction_label_legacy_no_chars():
    comp = {"messages_in_segment": 5, "synthesis": "x"}
    from gc_tui.services.trace_render import build_compaction_label

    assert build_compaction_label(2, comp) == "контекст сжат #2 · 5→1"


def test_block_panel_for_code_with_lang():
    pytest.importorskip("textual")
    from gc_tui.widgets.block_panel import BlockPanel

    w = BlockPanel.for_code("print(1)", "python", event_no=1)
    assert w.has_class("code-block-panel")


def test_block_panel_for_code_unknown_lang_fallback():
    pytest.importorskip("textual")
    from gc_tui.widgets.block_panel import BlockPanel

    w = BlockPanel.for_code("x = 1", "no-such-lang-xyz", event_no=2)
    assert w.has_class("code-block-panel")


def test_block_panel_for_code_no_lang_fallback():
    pytest.importorskip("textual")
    from gc_tui.widgets.block_panel import BlockPanel

    w = BlockPanel.for_code("plain text", None, event_no=3)
    assert w.has_class("code-block-panel")


def test_build_agent_widgets_prose():
    pytest.importorskip("textual")
    from gc_tui.services.trace_render import build_agent_widgets

    ws = build_agent_widgets("просто текст ответа", event_no=5)
    assert len(ws) == 1
    from gc_tui.widgets.trace_row import TraceRow

    assert isinstance(ws[0], TraceRow)


def test_build_agent_widgets_code_and_prose_order_and_focus():
    pytest.importorskip("textual")
    from gc_tui.services.trace_render import build_agent_widgets

    md = "вступление\n\n```python\nprint(1)\n```\n\nитог"
    ws = build_agent_widgets(md, event_no=7)
    # 3 виджета: proza, code-block, proza — в порядке
    assert len(ws) == 3
    assert ws[0].has_class("code-block-panel") is False  # первый proza
    assert ws[1].has_class("code-block-panel") is True  # code-block


def test_build_agent_widgets_empty_fence_yields_no_block_panel():
    """Е2: пустой fenced-блок не становится сегментом → без BlockPanel."""
    pytest.importorskip("textual")
    from gc_tui.services.trace_render import build_agent_widgets
    from gc_tui.widgets.block_panel import BlockPanel

    ws = build_agent_widgets("```\n\n```", event_no=1)
    assert not any(isinstance(w, BlockPanel) for w in ws)


def _env(payload, ts="2026-05-30T10:00:00Z"):
    return {
        "type": "message",
        "thread_id": "t",
        "system": "cli",
        "seq": 1,
        "ts": ts,
        "payload": payload,
    }


@pytest.mark.asyncio
async def test_render_thread_readonly_kinds():
    pytest.importorskip("textual")
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from gc_tui.widgets.user_reply import UserReplyContainer
    from textual.app import App
    from textual.containers import VerticalScroll
    from trace_readonly import render_thread_readonly

    records = [
        _env({"role": "user", "content": "привет", "id": "m1"}),
        _env(
            {
                "id": "a1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": "ответ агента",
                        "annotations": [],
                    }
                ],
            }
        ),
        _env(
            {
                "type": "function_call",
                "id": "fc1",
                "call_id": "c1",
                "name": "read_file",
                "arguments": '{"path": "x.py"}',
                "status": "completed",
            }
        ),
        _env(
            {
                "id": "fco1",
                "type": "function_call_output",
                "call_id": "c1",
                "output": '{"ok": true, "total_lines": 10}',
            }
        ),
    ]

    class _A(App):
        def compose(self):
            yield VerticalScroll(id="t")

        def on_mount(self):
            render_thread_readonly(
                self.query_one("#t", VerticalScroll), records
            )

    async with _A().run_test() as pilot:
        scr = pilot.app.screen
        assert len(scr.query(UserReplyContainer)) == 1
        # call→result спарены в одну ToolCallRow (output не отдельной строкой)
        assert len(scr.query(ToolCallRow)) == 1
        body = "\n".join(str(w.render()) for w in scr.query())
        assert "ответ агента" in body


@pytest.mark.asyncio
async def test_render_thread_readonly_call_row_uses_wire_tool_name():
    """Д11/R3: readonly-превью показывает wire-имя тула (как в реестре), не
    канонический id маппера — fs_append_file, не append_to_file."""
    pytest.importorskip("textual")
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from textual.app import App
    from textual.containers import VerticalScroll
    from trace_readonly import render_thread_readonly

    records = [
        _env(
            {
                "type": "function_call",
                "id": "fc1",
                "call_id": "c1",
                "name": "fs_append_file",
                "arguments": '{"path": "x.py", "content": "hi"}',
            }
        ),
    ]

    class _A(App):
        def compose(self):
            yield VerticalScroll(id="t")

        def on_mount(self):
            render_thread_readonly(
                self.query_one("#t", VerticalScroll), records
            )

    async with _A().run_test() as pilot:
        row = pilot.app.screen.query_one(ToolCallRow)
        assert row._tool_name == "fs_append_file"


def _seg(payloads, synthesis=None, n=0, index=0):
    comp = (
        None
        if synthesis is None
        else {"ts": "t", "messages_in_segment": n, "synthesis": synthesis}
    )
    return {
        "index": index,
        "messages": [_env(p) for p in payloads],
        "compacted_by": comp,
    }


@pytest.mark.asyncio
async def test_render_segments_readonly_parity():
    """Превью из сегментов = паритет с основным трейсом: hidden(policy) скрыт,
    маркер сжатия #K + синтез, intermediate_reply как ·, error_msg красным."""
    pytest.importorskip("textual")
    from gc_tui.services.trace_render import render_segments_readonly
    from gc_tui.widgets.trace_row import TraceRow
    from textual.app import App
    from textual.containers import VerticalScroll

    segments = [
        _seg(
            [
                {"role": "user", "content": "вопрос", "id": "m1"},
                {
                    "type": "function_call",
                    "id": "fc1",
                    "call_id": "c1",
                    "name": "intermediate_reply",
                    "arguments": '{"answer": "промежуточный"}',
                },
                {
                    "role": "user",
                    "id": "mp",
                    "content": '<technical type="technical">служебное</technical>',
                },
            ],
            synthesis="СИНТЕЗ-X",
            n=3,
            index=0,
        ),
        _seg(
            [
                {
                    "id": "a1",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "финал",
                            "annotations": [],
                        }
                    ],
                },
                {
                    "role": "user",
                    "id": "me",
                    "content": '<technical type="error_message">ошибка бэка</technical>',
                },
            ],
            index=1,
        ),
    ]

    class _A(App):
        def compose(self):
            yield VerticalScroll(id="t")

        def on_mount(self):
            render_segments_readonly(
                self.query_one("#t", VerticalScroll), segments
            )

    async with _A().run_test() as pilot:
        scr = pilot.app.screen
        body = "\n".join(str(w.render()) for w in scr.query())
        # policy/technical — скрыто (hidden), как на основном экране
        assert "служебное" not in body
        assert "[policy]" not in body
        # маркер сжатия #1 + синтез виден
        markers = [
            w for w in scr.query(TraceRow) if w.has_class("summary-marker")
        ]
        assert len(markers) == 1
        assert "контекст сжат #1" in body
        assert "СИНТЕЗ-X" in body
        # intermediate_reply — отдельная line-intermediate (·), не обычная реплика
        assert "промежуточный" in body
        assert (
            len(
                [
                    w
                    for w in scr.query(TraceRow)
                    if w.has_class("line-intermediate")
                ]
            )
            == 1
        )
        # error_message — красная строка
        assert "ошибка бэка" in body
        assert (
            len([w for w in scr.query(TraceRow) if w.has_class("line-error")])
            == 1
        )
        assert "финал" in body


def test_build_agent_widgets_has_bullet_prefix():
    pytest.importorskip("textual")
    from gc_tui.services.trace_render import build_agent_widgets

    ws = build_agent_widgets("просто текст", event_no=1)
    assert ws[0]._prefix == "● "


def test_build_intermediate_widgets_renders_code_block():
    """·-реплика проходит тот же markdown/код-пайплайн, что и проза: код-фенс
    становится панелью, а не сырой строкой с литеральными ```. Маркер «· »
    и класс line-intermediate сохранены."""
    pytest.importorskip("textual")
    from gc_tui.services.trace_render import build_intermediate_widgets

    md = "вступление\n\n```python\nprint(1)\n```"
    ws = build_intermediate_widgets(md, event_no=9)
    assert ws[0]._prefix == "· "
    assert ws[0].has_class("line-intermediate")
    assert any(w.has_class("code-block-panel") for w in ws)


def test_build_intermediate_widgets_plain_text_single_row():
    """Простой текст (без markdown) — по-прежнему одна line-intermediate строка
    (паритет со старым поведением, инвариант превью)."""
    pytest.importorskip("textual")
    from gc_tui.services.trace_render import build_intermediate_widgets
    from gc_tui.widgets.trace_row import TraceRow

    ws = build_intermediate_widgets("промежуточный ответ", event_no=1)
    assert len(ws) == 1 and isinstance(ws[0], TraceRow)
    assert ws[0]._prefix == "· " and ws[0].has_class("line-intermediate")


def test_build_block_widgets_no_bullet_prefix():
    pytest.importorskip("textual")
    from gc_tui.services.trace_render import build_block_widgets
    from gc_tui.widgets.trace_row import TraceRow

    ws = build_block_widgets("просто текст", prose_bullet=False, event_no=1)
    assert len(ws) == 1 and isinstance(ws[0], TraceRow)
    assert ws[0]._prefix == ""


def test_build_block_widgets_table_widget():
    pytest.importorskip("textual")
    from gc_tui.services.trace_render import build_block_widgets

    ws = build_block_widgets(
        "| A | B |\n|---|---|\n| 1 | 2 |", prose_bullet=True, event_no=1
    )
    assert len(ws) == 1
    assert ws[0].has_class("table-block")


def test_build_block_widgets_table_indent_only_with_bullet():
    """Трейс/превью (prose_bullet=True): таблица с отступом под текст (.table-indent).
    Баннер (prose_bullet=False): без отступа — вровень с прозой на колонке 0."""
    pytest.importorskip("textual")
    from gc_tui.services.trace_render import build_block_widgets

    md = "| A | B |\n|---|---|\n| 1 | 2 |"
    trace = build_block_widgets(md, prose_bullet=True, event_no=1)
    banner = build_block_widgets(md, prose_bullet=False, event_no=1)
    assert trace[0].has_class("table-indent")
    assert not banner[0].has_class("table-indent")
    assert trace[0].has_class("table-block") and banner[0].has_class(
        "table-block"
    )


def test_build_block_widgets_code_indent_only_with_bullet():
    """Код-панель: отступ под текст в трейсе/превью (.code-indent), без отступа в баннере
    (как таблицы) — чтобы код в ask_human был вровень с прозой на колонке 0."""
    pytest.importorskip("textual")
    from gc_tui.services.trace_render import build_block_widgets

    md = "```python\nprint(1)\n```"
    trace = build_block_widgets(md, prose_bullet=True, event_no=1)
    banner = build_block_widgets(md, prose_bullet=False, event_no=1)
    assert trace[0].has_class("code-indent")
    assert not banner[0].has_class("code-indent")
    assert trace[0].has_class("code-block-panel") and banner[0].has_class(
        "code-block-panel"
    )


@pytest.mark.asyncio
async def test_readonly_submit_note_label():
    """submit_solution.comment в превью → строка line-note «на заметку: …»."""
    pytest.importorskip("textual")
    import json as _json

    from gc_tui.services.trace_render import render_segments_readonly
    from gc_tui.widgets.trace_row import TraceRow
    from textual.app import App
    from textual.containers import VerticalScroll

    segments = [
        {
            "index": 0,
            "compacted_by": None,
            "messages": [
                _env(
                    {
                        "type": "function_call",
                        "id": "fc",
                        "call_id": "c",
                        "name": "submit_solution",
                        "arguments": _json.dumps(
                            {"task_report": "готово", "comment": "проверь X"}
                        ),
                    }
                )
            ],
        }
    ]

    class _A(App):
        def compose(self):
            yield VerticalScroll(id="t")

        def on_mount(self):
            render_segments_readonly(
                self.query_one("#t", VerticalScroll), segments
            )

    async with _A().run_test() as pilot:
        notes = [
            w
            for w in pilot.app.screen.query(TraceRow)
            if w.has_class("line-note")
        ]
        assert len(notes) == 1
        assert notes[0]._content.plain == "заметка: проверь X"


def test_code_block_builds_block_panel():
    pytest.importorskip("textual")
    from gc_tui.services.trace_render import build_block_widgets
    from gc_tui.widgets.block_panel import BlockPanel

    widgets = build_block_widgets(
        "текст\n\n```python\nx = 1\n```", prose_bullet=True, event_no=5
    )
    panels = [w for w in widgets if isinstance(w, BlockPanel)]
    assert len(panels) == 1
    assert panels[0].has_class("code-block-panel")
    assert panels[0].has_class("code-indent")


def test_table_builds_block_panel():
    pytest.importorskip("textual")
    from gc_tui.services.trace_render import build_block_widgets
    from gc_tui.widgets.block_panel import BlockPanel

    text = "до\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
    panels = [
        w
        for w in build_block_widgets(text, prose_bullet=True, event_no=6)
        if isinstance(w, BlockPanel)
    ]
    assert len(panels) == 1
    assert panels[0].has_class("table-block")


def test_banner_blocks_have_no_indent():
    pytest.importorskip("textual")
    from gc_tui.services.trace_render import build_block_widgets
    from gc_tui.widgets.block_panel import BlockPanel

    widgets = build_block_widgets(
        "```python\nx = 1\n```", prose_bullet=False, event_no=1
    )
    panel = next(w for w in widgets if isinstance(w, BlockPanel))
    assert not panel.has_class("code-indent")


def test_first_widget_is_the_only_tab_stop():
    pytest.importorskip("textual")
    from gc_tui.services.trace_render import build_block_widgets

    widgets = build_block_widgets(
        "проза\n\n```python\nx = 1\n```\n\nещё проза",
        prose_bullet=True,
        event_no=5,
    )
    assert [w.can_focus for w in widgets] == [True, False, False]


def test_event_no_bound_to_every_widget():
    pytest.importorskip("textual")
    from gc_tui.services.trace_render import build_block_widgets

    widgets = build_block_widgets(
        "проза\n\n```python\nx = 1\n```", prose_bullet=True, event_no=9
    )
    assert all(w._event_no == 9 for w in widgets)


@pytest.mark.asyncio
async def test_intermediate_reply_row_has_top_margin(tmp_path):
    """`·`-реплика (intermediate_reply) — фаза хода, как ●-проза:
    пустая строка сверху (margin-top: 1), не прилипает к предыдущей строке."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.trace_row import TraceRow
    from textual.containers import VerticalScroll

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        row = TraceRow(
            prefix="· ",
            content="промежуточный ответ",
            kind_class="line-intermediate",
        )
        scr.query_one("#trace", VerticalScroll).mount(row)
        await pilot.pause()
        assert row.styles.margin.top == 1


def test_dispatch_readonly_renders_todo_checklist():
    """C1: read-only preview должен рендерить session_todo как чек-лист
    (паритет с живым путём main.py), а не ронять в generic line-meta."""
    pytest.importorskip("textual")
    import json

    from gc_tui.services.trace_mapper import map_message
    from gc_tui.services.trace_render import _dispatch_readonly
    from gc_tui.widgets.trace_row import TraceRow

    ev = map_message(
        {
            "type": "function_call",
            "name": "session_todo",
            "call_id": "c1",
            "arguments": json.dumps(
                {
                    "todos": [
                        {"content": "Изучить", "status": "completed"},
                        {"content": "Тест", "status": "in_progress"},
                    ]
                },
                ensure_ascii=False,
            ),
        },
        no=1,
        ts="t",
    )
    assert ev.kind == "todo"

    mounted = []

    class _FakeTarget:
        def mount(self, w):
            mounted.append(w)

    _dispatch_readonly(_FakeTarget(), ev, {})
    # v5: чек-лист — один блок-виджет (единица выделения), строки внутри него.
    assert len(mounted) == 1
    block = mounted[0]
    assert block.has_class("todo-block") and block.can_focus
    rows = block._rows
    assert len(rows) == 3
    assert all(isinstance(row, TraceRow) for row in rows)
    assert rows[0].has_class("line-todo")  # НЕ line-meta


def test_kind_literal_includes_todo():
    """C2: тип Kind должен объявлять 'todo' (честность контракта)."""
    from typing import get_args

    from gc_tui.models import Kind

    assert "todo" in get_args(Kind)


def test_todo_event_renders_rich_text_with_glyph_colors():
    """kind=todo → TraceRow с rich.Text (канон §2), а не plain-строкой."""
    from gc_tui.models import TraceEvent
    from gc_tui.services.trace_render import _dispatch_readonly
    from rich.text import Text as RichText

    todos = [
        {"content": "Шаг раз", "status": "completed"},
        {"content": "Шаг два", "status": "in_progress"},
    ]
    payload = {
        "type": "function_call",
        "name": "session_todo",
        "arguments": {"todos": todos},
    }
    ev = TraceEvent(
        no=1,
        event_ts_utc_iso="",
        kind="todo",
        title="session_todo",
        summary="чек-лист 1/2",
        full_payload=payload,
        call_id="c1",
        tool_name="session_todo",
    )

    mounted = []

    class _Target:
        def mount(self, w, **kw):
            mounted.append(w)

    _dispatch_readonly(_Target(), ev, calls={})
    # v5: один блок-виджет; строки внутри — заголовок + строка на пункт.
    assert len(mounted) == 1
    rows = mounted[0]._rows
    header = rows[0]._content
    assert isinstance(header, RichText)
    assert header.plain == "session_todo   чек-лист 1/2"
    # Текст пункта — в content-колонке, отступ+глиф — в prefix: перенос
    # длинного пункта выравнивается под началом текста.
    assert rows[1]._prefix.plain == "     ✓ "
    assert rows[1]._content.plain == "Шаг раз"
    assert rows[2]._prefix.plain == "     → "
    assert rows[2]._content.plain == "Шаг два"
    # Tab-стоп один — сам блок; строки внутри не фокусируются.
    assert mounted[0].can_focus and not any(r.can_focus for r in rows)


def test_readonly_error_msg_finalizes_its_call_row():
    """Схемная ошибка вызова (technical error_message с call_id) обязана
    финализировать строку вызова ✗, как в живом трейсе (_render_error_msg).

    Иначе в превью строка навсегда остаётся «выполняется» (пульсирующий ▸),
    а ошибка уезжает отдельной [!]-строкой — так выглядел битый session_todo.
    """
    pytest.importorskip("textual")
    from gc_tui.models import TraceEvent
    from gc_tui.services.trace_render import _dispatch_readonly
    from gc_tui.widgets.tool_call_row import ToolCallRow

    mounted = []

    class _Target:
        children = ()  # is_tool_group_start смотрит на потомков контейнера

        def mount(self, w, **kw):
            mounted.append(w)

    calls: dict = {}
    call_ev = TraceEvent(
        no=1,
        event_ts_utc_iso="",
        kind="call",
        title="session_todo",
        summary="session_todo(todos=<list 1>)",
        full_payload={},
        call_id="c1",
        tool_name="session_todo",
    )
    _dispatch_readonly(_Target(), call_ev, calls)
    err_ev = TraceEvent(
        no=2,
        event_ts_utc_iso="",
        kind="error_msg",
        title="",
        summary="Wrong arguments for `session_todo`",
        full_payload={},
        call_id="c1",
    )
    _dispatch_readonly(_Target(), err_ev, calls)

    assert len(mounted) == 1, "ошибка не должна добавлять отдельную [!]-строку"
    row = mounted[0]
    assert isinstance(row, ToolCallRow)
    assert row._is_error is True and row._result is not None


def test_readonly_error_msg_without_call_row_still_shown():
    """Ошибка без известного вызова — прежняя [!]-строка, ничего не теряем."""
    pytest.importorskip("textual")
    from gc_tui.models import TraceEvent
    from gc_tui.services.trace_render import _dispatch_readonly

    mounted = []

    class _Target:
        def mount(self, w, **kw):
            mounted.append(w)

    ev = TraceEvent(
        no=1,
        event_ts_utc_iso="",
        kind="error_msg",
        title="",
        summary="что-то сломалось",
        full_payload={},
        call_id="unknown",
    )
    _dispatch_readonly(_Target(), ev, {})
    assert len(mounted) == 1


def test_note_text_label_bold_text_italic():
    from gc_tui.services.trace_render import note_text

    t = note_text("проверь X")

    assert t.plain == "заметка: проверь X"
    assert [(t.plain[s.start : s.end], str(s.style)) for s in t.spans] == [
        ("заметка:", "bold not italic"),
        ("проверь X", "italic"),
    ]


def test_summary_widgets_keep_code_block():
    pytest.importorskip("textual")
    from gc_tui.services.trace_render import build_summary_widgets
    from gc_tui.widgets.block_panel import BlockPanel

    ws = build_summary_widgets(
        "Сделал:\n\n```python\nx = 1\n```\nНе проверял.", event_no=3
    )

    assert [type(w).__name__ for w in ws] == [
        "TraceRow",
        "BlockPanel",
        "TraceRow",
    ]
    assert isinstance(ws[1], BlockPanel)
    assert ws[0]._prefix == "* "
    assert ws[0]._content.plain == "итог: Сделал:"
    assert ws[2]._content.plain == "Не проверял."
    assert ws[0].has_class("line-summary")
    assert ws[2].has_class("line-summary")
    label = ws[0]._content.spans[0]
    assert (label.start, label.end, str(label.style)) == (
        0,
        5,
        "bold not italic",
    )
    # Цвет текста — от CSS-класса записи (приглушённый), не цвет ответа.
    assert all(
        str(s.style) in ("", "bold not italic")
        for w in (ws[0], ws[2])
        for s in w._content.spans
    )


def test_dispatch_readonly_finish_after_reply_is_summary():
    """Превью: отчёт после текста того же ответа — запись «итог», не
    второй ответ; comment — второй строкой той же записи."""
    pytest.importorskip("textual")
    import json

    from gc_tui.services.trace_mapper import (
        collect_render_context,
        map_message,
    )
    from gc_tui.services.trace_render import _dispatch_readonly

    finish = {
        "type": "function_call",
        "name": "turn_finish",
        "call_id": "f1",
        "arguments": json.dumps(
            {"task_report": "Попрощался.", "comment": "Без тулов."}
        ),
    }
    reply = {"type": "message", "role": "assistant", "content": "Пока!"}
    ev = map_message(
        finish,
        no=2,
        ts="t",
        context=collect_render_context([reply, finish]),
    )
    mounted = []

    class _Target:
        def mount(self, w):
            mounted.append(w)

    _dispatch_readonly(_Target(), ev, {})

    assert [w._content.plain for w in mounted] == [
        "итог: Попрощался.\nБез тулов."
    ]
    assert mounted[0].has_class("line-summary")


@pytest.mark.asyncio
async def test_summary_row_styled_like_note_in_italic(tmp_path):
    """Запись «итог»: цвет и отступ строки заметки, текст курсивом."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.services.trace_render import build_summary_widgets
    from gc_tui.widgets.trace_row import TraceRow
    from textual.containers import VerticalScroll

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        trace = pilot.app.screen.query_one("#trace", VerticalScroll)
        note = TraceRow(prefix="* ", content="x", kind_class="line-note")
        (row,) = build_summary_widgets("Попрощался.", event_no=1)
        trace.mount(note)
        trace.mount(row)
        await pilot.pause()

        assert row.styles.color == note.styles.color
        assert row.styles.margin.top == 1
        assert row.styles.text_style.italic
        # Маркер «* » прямой, как у строки заметки; курсив — только текст.
        bullet = row.query_one(".trace-prefix")
        assert not bullet.rich_style.italic
        assert not note.query_one(".trace-prefix").rich_style.italic


def test_summary_bold_and_heading_have_no_accent_colour():
    pytest.importorskip("textual")
    from gc_tui.palette import ACCENT
    from gc_tui.services.trace_render import (
        build_agent_widgets,
        build_summary_widgets,
    )

    text = "# Заголовок\n\nЭто **важно**."
    (summary,) = build_summary_widgets(text, event_no=1)
    styles = {str(sp.style) for sp in summary._content.spans}
    assert "bold" in styles
    assert not any(ACCENT in st for st in styles)
    (agent,) = build_agent_widgets(text, event_no=1)
    agent_styles = {str(sp.style) for sp in agent._content.spans}
    assert f"{ACCENT} bold" in agent_styles
