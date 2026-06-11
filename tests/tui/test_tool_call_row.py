"""ToolCallRow — один виджет, который мутирует от running к ok/err."""
import pytest

from tui.palette import TOOL_NAME


_TOOL_NAME_HEX = TOOL_NAME[1:].lower()  # '#XXXXXX' → 'xxxxxx' для substring-проверок в span.style


@pytest.mark.asyncio
async def test_tool_call_row_initial_state():
    pytest.importorskip("textual")
    from textual.app import App, ComposeResult
    from tui.widgets.tool_call_row import ToolCallRow

    class _App(App):
        def compose(self) -> ComposeResult:
            yield ToolCallRow(call_id="c1", tool_name="read_file",
                              args_summary="path='src/x.py'")

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        text = str(row.query_one(".trace-content").render())
        assert "read_file" in text
        assert "src/x.py" in text
        assert "[..]" in text or "running" in text


@pytest.mark.asyncio
async def test_tool_call_row_mutate_to_ok():
    pytest.importorskip("textual")
    from textual.app import App, ComposeResult
    from tui.widgets.tool_call_row import ToolCallRow

    class _App(App):
        def compose(self) -> ComposeResult:
            yield ToolCallRow(call_id="c1", tool_name="read_file",
                              args_summary="path='src/x.py'")

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.set_result(summary="312 строк", error=False)
        await pilot.pause()
        text = str(row.query_one(".trace-content").render())
        assert "[ok]" in text
        assert "312 строк" in text
        assert "[..]" not in text and "running" not in text


@pytest.mark.asyncio
async def test_tool_call_row_mutate_to_error():
    pytest.importorskip("textual")
    from textual.app import App, ComposeResult
    from tui.widgets.tool_call_row import ToolCallRow

    class _App(App):
        def compose(self) -> ComposeResult:
            yield ToolCallRow(call_id="c2", tool_name="read_file",
                              args_summary="path='src/missing.py'")

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.set_result(summary="ENOENT: нет файла", error=True)
        await pilot.pause()
        text = str(row.query_one(".trace-content").render())
        assert "[!]" in text
        assert "ENOENT" in text


@pytest.mark.asyncio
async def test_tool_call_row_brackets_in_args_not_interpreted_as_markup():
    """Args containing [brackets] must render literally (Content not str)."""
    pytest.importorskip("textual")
    from textual.app import App, ComposeResult
    from tui.widgets.tool_call_row import ToolCallRow

    class _App(App):
        def compose(self) -> ComposeResult:
            yield ToolCallRow(
                call_id="c1",
                tool_name="search_files",
                args_summary="pattern='[bold]x[/bold]'",
            )

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        text = str(row.query_one(".trace-content").render())
        # Brackets must appear literally — no markup parsing
        assert "[bold]" in text
        assert "[/bold]" in text


def test_tool_call_row_exposes_call_id():
    """call_id must be a public attribute (Task 2.3 looks up widgets by it)."""
    from tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="abc-123", tool_name="t", args_summary="")
    assert row.call_id == "abc-123"


@pytest.mark.asyncio
async def test_tool_call_row_ok_with_empty_summary_omits_arrow():
    """Result с пустым summary рендерится как `name(args) [ok]` без `-> `."""
    pytest.importorskip("textual")
    from textual.app import App, ComposeResult
    from tui.widgets.tool_call_row import ToolCallRow

    class _App(App):
        def compose(self) -> ComposeResult:
            yield ToolCallRow(call_id="c1", tool_name="skills_load",
                              args_summary="skill_id='x'")

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.set_result(summary="", error=False)
        await pilot.pause()
        text = str(row.query_one(".trace-content").render())
        assert "[ok]" in text
        assert "-> " not in text  # стрелки нет при пустом summary
        assert "skills_load" in text


@pytest.mark.asyncio
async def test_tool_call_row_ok_state_uses_styled_text():
    """OK state renders as rich.text.Text with colored span for `[ok]` (canon §3.1)."""
    pytest.importorskip("textual")
    from textual.app import App, ComposeResult
    from rich.text import Text
    from tui.widgets.tool_call_row import ToolCallRow

    class _App(App):
        def compose(self) -> ComposeResult:
            yield ToolCallRow(call_id="c1", tool_name="read_file",
                              args_summary="path='x.py'")

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.set_result(summary="100 строк", error=False)
        await pilot.pause()
        rendered = row._render_content()

        assert isinstance(rendered, Text)
        # Должны быть отдельные стилизованные сегменты (dim args + green [ok])
        assert len(rendered.spans) >= 2

        # Найти span со стилем ok-green (#5FAE7F) и убедиться что он покрывает "[ok]"
        ok_color = "#5FAE7F"
        plain = rendered.plain
        green_segments = [
            plain[s.start:s.end]
            for s in rendered.spans
            if str(s.style).lower() == ok_color.lower()
        ]
        assert any("[ok]" in seg for seg in green_segments), (
            f"No green span containing [ok]; spans={rendered.spans}"
        )


@pytest.mark.asyncio
async def test_tool_call_row_tool_name_uses_tool_name_color():
    """Имя тула (без скобок) красится palette.TOOL_NAME — primary-сигнал в строке;
    скобки/args/путь — DIM (один общий тон обвеса)."""
    pytest.importorskip("textual")
    from textual.app import App, ComposeResult
    from rich.text import Text
    from tui.widgets.tool_call_row import ToolCallRow

    class _App(App):
        def compose(self) -> ComposeResult:
            yield ToolCallRow(call_id="c1", tool_name="read_file",
                              args_summary="path='src/thread_store.py'")

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.set_result(summary="312 строк", error=False)
        await pilot.pause()
        rendered = row._render_content()
        assert isinstance(rendered, Text)
        tool_name_spans = [
            rendered.plain[s.start:s.end]
            for s in rendered.spans
            if _TOOL_NAME_HEX in str(s.style).lower()
        ]
        assert "read_file" in tool_name_spans, (
            f"No TOOL_NAME-colored span equal to 'read_file'; spans="
            f"{[(rendered.plain[s.start:s.end], str(s.style)) for s in rendered.spans]}"
        )


@pytest.mark.asyncio
async def test_tool_call_row_set_result_twice_overwrites():
    """Second set_result replaces the first state (matters for stream_reset re-stream)."""
    pytest.importorskip("textual")
    from textual.app import App, ComposeResult
    from tui.widgets.tool_call_row import ToolCallRow

    class _App(App):
        def compose(self) -> ComposeResult:
            yield ToolCallRow(call_id="c1", tool_name="read_file",
                              args_summary="path='src/x.py'")

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.set_result(summary="initial", error=False)
        await pilot.pause()
        row.set_result(summary="updated", error=True)
        await pilot.pause()

        text = str(row.query_one(".trace-content").render())
        assert "updated" in text
        assert "[!]" in text
        assert "initial" not in text
        assert "[ok]" not in text


@pytest.mark.asyncio
async def test_double_click_posts_open_tool_call_detail_request():
    pytest.importorskip("textual")
    from textual import events
    from tui.widgets.tool_call_row import ToolCallRow
    from tui.messages import OpenToolCallDetailRequest

    captured: list = []

    from textual.app import App

    class _App(App):
        def compose(self):
            yield ToolCallRow(call_id="c42", tool_name="read_file",
                              args_summary="", event_no=1)

        def on_open_tool_call_detail_request(self, message):
            captured.append(message.call_id)

    app = _App()
    async with app.run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        click = events.Click(
            widget=row, x=0, y=0, delta_x=0, delta_y=0, button=1,
            shift=False, meta=False, ctrl=False, chain=2,
        )
        row.on_click(click)
        await pilot.pause()

    assert captured == ["c42"]


@pytest.mark.asyncio
async def test_focus_change_posts_tool_call_focus_changed():
    pytest.importorskip("textual")
    from tui.widgets.tool_call_row import ToolCallRow

    received: list = []

    from textual.app import App

    class _App(App):
        def compose(self):
            yield ToolCallRow(call_id="c7", tool_name="read_file",
                              args_summary="", event_no=1)
            from textual.widgets import Input
            yield Input(id="sink")

        def on_tool_call_focus_changed(self, message):
            received.append((message.call_id, message.focused))

    app = _App()
    async with app.run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.focus()
        await pilot.pause()
        pilot.app.query_one("#sink").focus()  # вызовет blur у row
        await pilot.pause()

    # Должны увидеть focused=True, потом focused=False для того же call_id
    assert ("c7", True) in received
    assert ("c7", False) in received


@pytest.mark.asyncio
async def test_focus_renders_event_number_prefix():
    """Regression: f750f3f (ToolCallRow compose-рефактор) инвертировал `[N]`-on-focus.

    compose-based widget читал has_focus синхронно в _on_focus ДО того, как
    Widget._on_focus (parent в MRO) ставил has_focus=True → `[N]` не появлялся
    при фокусе и оставался после blur. Этот тест ловит обе ветки симптома.
    """
    pytest.importorskip("textual")
    from textual.app import App, ComposeResult
    from textual.widgets import Static
    from tui.widgets.tool_call_row import ToolCallRow

    class _App(App):
        # AUTO_FOCUS=None — обходим Screen._update_auto_focus, который ставит
        # has_focus=True напрямую (textual/screen.py:1498) и маскирует баг
        # инверсии. Фокус идёт ТОЛЬКО через post_message(Focus) → dispatcher,
        # то есть тот самый buggy путь, на котором живёт production-симптом.
        AUTO_FOCUS = None

        def compose(self) -> ComposeResult:
            yield ToolCallRow(call_id="c1", tool_name="read_file",
                              args_summary="path='x'", event_no=42)
            yield Static("decoy", id="decoy")

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        content = row.query_one(".trace-content")

        # До фокуса — [42] нет.
        assert "[42]" not in str(content.render())

        row.focus()
        await pilot.pause(0.05)
        assert row.has_focus
        # На фокусе — [42] есть.
        assert "[42]" in str(content.render())

        pilot.app.set_focus(None)
        await pilot.pause(0.05)
        # После blur — [42] нет.
        assert "[42]" not in str(content.render())


@pytest.mark.asyncio
async def test_focus_handler_has_focus_after_super():
    """Контракт: `super()._on_focus(event)` ставит has_focus=True ДО возврата.

    Anti-regression: если кто-то снова уберёт super()._on_focus(event),
    has_focus останется False внутри subclass-handler-а — этот тест упадёт.
    """
    pytest.importorskip("textual")
    from textual.app import App, ComposeResult
    from tui.widgets.tool_call_row import ToolCallRow

    captured = {"has_focus_after_super": None}

    class _ProbeRow(ToolCallRow):
        def _on_focus(self, event):
            super()._on_focus(event)
            captured["has_focus_after_super"] = self.has_focus

    class _App(App):
        def compose(self) -> ComposeResult:
            yield _ProbeRow(call_id="c1", tool_name="read_file",
                            args_summary="path='x'", event_no=1)

    async with _App().run_test() as pilot:
        pilot.app.query_one(_ProbeRow).focus()
        await pilot.pause(0.05)

    assert captured["has_focus_after_super"] is True


def test_running_row_shows_elapsed_seconds():
    import time
    from tui.widgets.tool_call_row import ToolCallRow
    row = ToolCallRow(call_id="c1", tool_name="subagent_planner", args_summary="task=x")
    row._start_monotonic = time.monotonic() - 47.0
    r = str(row._render_content())
    assert "running · 47с" in r
    assert "running..." not in r


def test_running_row_elapsed_same_for_any_tool():
    import time
    from tui.widgets.tool_call_row import ToolCallRow
    row = ToolCallRow(call_id="c2", tool_name="read_file", args_summary="path=a")
    row._start_monotonic = time.monotonic() - 3.0
    assert "running · 3с" in str(row._render_content())


def test_completed_ok_row_has_no_elapsed():
    from tui.widgets.tool_call_row import ToolCallRow
    row = ToolCallRow(call_id="c3", tool_name="subagent_planner", args_summary="x")
    row.set_result(summary="ok", error=False)
    r = str(row._render_content())
    assert "[ok]" in r
    assert "running" not in r


def test_completed_error_row_has_no_elapsed():
    from tui.widgets.tool_call_row import ToolCallRow
    row = ToolCallRow(call_id="c4", tool_name="subagent_planner", args_summary="x")
    row.set_result(summary="boom", error=True)
    r = str(row._render_content())
    assert "[!]" in r
    assert "running" not in r


def test_refresh_running_elapsed_noop_after_completion():
    from tui.widgets.tool_call_row import ToolCallRow
    row = ToolCallRow(call_id="c5", tool_name="subagent_planner", args_summary="x")
    row.set_result(summary="ok", error=False)
    row.refresh_running_elapsed()  # не падает, не возвращает в running
    assert "[ok]" in str(row._render_content())


@pytest.mark.asyncio
async def test_poll_active_ticks_running_rows():
    pytest.importorskip("textual")
    import time
    from textual.containers import VerticalScroll
    from tui.app import TuiApp
    from tui.widgets.tool_call_row import ToolCallRow
    class _LiveRunner:  # без живого run инвариант поллинга финализирует строку
        def is_running(self) -> bool:
            return True

    app = TuiApp()
    app.active_runner = _LiveRunner()
    async with app.run_test() as pilot:
        scr = pilot.app.screen  # TraceScreen (дефолтный экран)
        row = ToolCallRow(call_id="c1", tool_name="subagent_planner", args_summary="task=x")
        scr.query_one("#trace", VerticalScroll).mount(row)
        await pilot.pause()                       # compose → _content_static готов (running · 0с)
        scr._active_calls["c1"] = row
        row._start_monotonic = time.monotonic() - 5.0
        scr._poll_active()                        # должен дёрнуть refresh у running-строк
        await pilot.pause()
        assert "running · 5с" in str(row._content_static.render())


# ---- mark_interrupted: терминальное состояние «прервано» (spec 2026-06-10) ----

def test_mark_interrupted_renders_dim_terminal_state():
    from tui.widgets.tool_call_row import ToolCallRow
    row = ToolCallRow(call_id="c6", tool_name="subagent_critic", args_summary="t")
    row.mark_interrupted()
    r = str(row._render_content())
    assert "прервано" in r
    assert "running" not in r and "[..]" not in r
    assert "[ok]" not in r and "[!]" not in r


def test_mark_interrupted_noop_after_result_and_idempotent():
    from tui.widgets.tool_call_row import ToolCallRow
    row = ToolCallRow(call_id="c7", tool_name="read_file", args_summary="p")
    row.set_result(summary="312 строк", error=False)
    row.mark_interrupted()                      # после результата — no-op
    assert "[ok]" in str(row._render_content())
    row2 = ToolCallRow(call_id="c8", tool_name="read_file", args_summary="p")
    row2.mark_interrupted()
    row2.mark_interrupted()                     # повторный вызов — no-op
    assert "прервано" in str(row2._render_content())


def test_refresh_running_elapsed_noop_after_interrupted():
    from tui.widgets.tool_call_row import ToolCallRow
    row = ToolCallRow(call_id="c9", tool_name="exec", args_summary="")
    row.mark_interrupted()
    row.refresh_running_elapsed()               # не возвращает в running
    r = str(row._render_content())
    assert "прервано" in r and "running" not in r
