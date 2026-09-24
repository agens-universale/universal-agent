"""Integration-тесты layout трассы: TraceRow прокинут везде где должен быть."""

import pytest


@pytest.mark.asyncio
async def test_think_not_rendered_via_trace_row():
    """Reasoning suppressed: kind=system/title=think does not mount any .line-think row."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.models import TraceEvent
    from gc_tui.widgets.trace_row import TraceRow

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        ev = TraceEvent(
            no=1,
            event_ts_utc_iso="2026-05-28T00:00:00Z",
            kind="system",
            title="think",
            summary="рассуждаю",
            tool_name=None,
            call_id=None,
            full_payload=None,
        )
        scr._render_event(ev)
        await pilot.pause()
        rows = scr.query(TraceRow)
        think_rows = [r for r in rows if r.has_class("line-think")]
        assert len(think_rows) == 0


@pytest.mark.asyncio
async def test_submit_solution_note_renders_via_trace_row():
    """* note: <comment> монтируется как TraceRow с prefix='  * '."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.models import TraceEvent
    from gc_tui.widgets.trace_row import TraceRow

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        ev = TraceEvent(
            no=1,
            event_ts_utc_iso="2026-05-28T00:00:00Z",
            kind="agent",
            title=None,
            summary="готов",
            tool_name="submit_solution",
            call_id="c1",
            full_payload={"_submit_comment": "проверь корнер-кейсы"},
        )
        scr._render_event(ev)
        await pilot.pause()
        note_rows = [
            r for r in scr.query(TraceRow) if r.has_class("line-note")
        ]
        assert len(note_rows) == 1
        assert note_rows[0]._prefix == "* "
        content = note_rows[0]._content
        assert content.plain == "заметка: проверь корнер-кейсы"
        # ярлык «заметка:» прямой, текст ремарки — курсивом
        italic = [s for s in content.spans if "italic" in str(s.style).lower()]
        assert italic and all(s.start >= len("заметка: ") for s in italic)


@pytest.mark.asyncio
async def test_agent_prose_first_block_has_bullet_prefix():
    """Первый prose-блок агентской реплики имеет prefix '● '."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.models import TraceEvent
    from gc_tui.widgets.trace_row import TraceRow

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        ev = TraceEvent(
            no=1,
            event_ts_utc_iso="2026-05-28T00:00:00Z",
            kind="agent",
            title=None,
            summary="первая строка\n\nвторая строка",
            tool_name=None,
            call_id=None,
            full_payload=None,
        )
        scr._render_event(ev)
        await pilot.pause()
        rows = list(scr.query(TraceRow))
        agent_rows = [r for r in rows if r.has_class("line-agent")]
        # Проза, разделённая только пустыми строками, теперь рисуется ОДНИМ prose-
        # блоком (пустые строки сохранены внутри) — сплит только на code/table (0ff36ad8).
        assert len(agent_rows) == 1
        assert agent_rows[0]._prefix == "● "
        assert not agent_rows[0].has_class(
            "line-agent-cont"
        )  # cont — только за code/table


@pytest.mark.asyncio
async def test_tool_call_row_has_two_column_layout():
    """ToolCallRow содержит Horizontal с prefix (отступ-индент + статус-глиф, без '|-') и content."""
    pytest.importorskip("textual")
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from textual.app import App, ComposeResult

    class _App(App):
        def compose(self) -> ComposeResult:
            yield ToolCallRow(
                call_id="c1", tool_name="read_file", args_summary="path='x'"
            )

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        prefix = row.query_one(".trace-prefix")
        content = row.query_one(".trace-content")
        assert "|-" not in str(prefix.render())  # лесенки больше нет
        assert "▸" in str(prefix.render())  # статус-глиф running в prefix
        assert "read_file" in str(content.render())
        assert "|- " not in str(
            content.render()
        )  # prefix НЕ дублируется в content


@pytest.mark.asyncio
async def test_long_tool_args_wrap_inside_content_column():
    """Длинные args не уезжают к левому краю — wrap внутри content-колонки.

    Проверяем структурно: prefix-Static и content-Static — отдельные виджеты,
    значит wrap content не выходит за границы своей колонки (rendering Textual'а)."""
    pytest.importorskip("textual")
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from textual.app import App, ComposeResult

    long_args = "command='" + ("very_long_path/" * 20) + "x.py'"

    class _App(App):
        def compose(self) -> ComposeResult:
            yield ToolCallRow(
                call_id="c1",
                tool_name="execute_command",
                args_summary=long_args,
            )

    async with _App().run_test(size=(80, 24)) as pilot:
        row = pilot.app.query_one(ToolCallRow)
        # Прокрутили рендер хотя бы 1 раз
        await pilot.pause()
        prefix = row.query_one(".trace-prefix")
        content = row.query_one(".trace-content")
        # prefix — ровный отступ-индент + статус-глиф (без «лесенки»), не зависит от длины content
        assert "|-" not in str(prefix.render())
        assert str(prefix.render()).startswith("   ")
        # content содержит все длинные args (wrap'нутые, но в одном Static)
        rendered_content = str(content.render())
        assert "execute_command" in rendered_content
        assert "very_long_path" in rendered_content


@pytest.mark.asyncio
async def test_meta_lines_have_top_margin_class():
    """Все framework-события имеют CSS-класс .line-meta (margin-top: 1 из styles.tcss)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.trace_row import TraceRow

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        # Триггерим экспорт-сообщение через прямой вызов фабрики
        scr._mount_trace_row(
            prefix="[exported] ", content="/tmp/x.md", kind_class="line-meta"
        )
        await pilot.pause()
        meta_rows = [
            r for r in scr.query(TraceRow) if r.has_class("line-meta")
        ]
        assert len(meta_rows) >= 1


@pytest.mark.asyncio
async def test_todo_row_has_air_above_and_no_double_gap():
    """Чек-лист session_todo обособлен пустой строкой СВЕРХУ (конвенция трейса:
    разделение через margin-top следующего элемента), и ровно одной — не двумя."""
    pytest.importorskip("textual")
    import json

    from gc_tui.app import TuiApp
    from gc_tui.models import TraceEvent

    def call_ev(no, name, args):
        return TraceEvent(
            no=no,
            event_ts_utc_iso="2026-07-30T00:00:00Z",
            kind="call",
            title=name,
            summary=f"{name}(...)",
            tool_name=name,
            call_id=f"c{no}",
            full_payload={"arguments": json.dumps(args)},
        )

    todo_ev = TraceEvent(
        no=2,
        event_ts_utc_iso="2026-07-30T00:00:00Z",
        kind="todo",
        title="session_todo",
        summary="чек-лист 1/2\n  ✓ Шаг 1\n  → Шаг 2",
        tool_name="session_todo",
        call_id="c2",
        full_payload=None,
    )

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        scr._render_event(call_ev(1, "fs_read_file", {"path": "a.py"}))
        scr._render_event(todo_ev)
        scr._render_event(call_ev(3, "exec_command", {"command": "pytest"}))
        await pilot.pause()

        # v5: воздух живёт на блоке-контейнере, а не на строке заголовка.
        from gc_tui.widgets.todo_block import TodoBlock

        blocks = list(scr.query(TodoBlock))
        assert len(blocks) == 1
        todo = blocks[0]
        assert todo.styles.margin.top == 1, "нужен отступ сверху"
        assert todo.styles.margin.bottom == 0, (
            "нижнего отступа быть не должно: он сложится с margin-top следующего "
            "элемента (конвенция трейса, см. .user-band в styles.tcss)"
        )
        # Заголовок внутри блока собственного воздуха не несёт (иначе сложится).
        header = todo.todo_rows[0]
        assert header.has_class("line-todo") and header.styles.margin.top == 0
