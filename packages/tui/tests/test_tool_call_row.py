"""ToolCallRow — один виджет, который мутирует от running к ok/err."""

import pytest
from gc_tui.palette import TOOL_NAME

_TOOL_NAME_HEX = TOOL_NAME[1:].lower()  # '#XXXXXX' → 'xxxxxx' для substring-проверок в span.style


@pytest.mark.asyncio
async def test_tool_call_row_initial_state():
    pytest.importorskip("textual")
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from textual.app import App, ComposeResult

    class _App(App):
        def compose(self) -> ComposeResult:
            yield ToolCallRow(call_id="c1", tool_name="read_file", args_summary="path='src/x.py'")

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        content = str(row.query_one(".trace-content").render())
        prefix = str(row.query_one(".trace-prefix").render())
        assert "read_file" in content
        assert "src/x.py" in content
        assert "▸" in prefix  # running-глиф ведёт строку (prefix)
        assert "running" not in content  # слово убрано


@pytest.mark.asyncio
async def test_tool_call_row_mutate_to_ok():
    pytest.importorskip("textual")
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from textual.app import App, ComposeResult

    class _App(App):
        def compose(self) -> ComposeResult:
            yield ToolCallRow(call_id="c1", tool_name="read_file", args_summary="path='src/x.py'")

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.set_result(summary="312 строк", error=False)
        await pilot.pause()
        content = str(row.query_one(".trace-content").render())
        prefix = str(row.query_one(".trace-prefix").render())
        assert "✓" in prefix
        assert "312 строк" in content
        assert "▸" not in prefix and "running" not in content


@pytest.mark.asyncio
async def test_tool_call_row_mutate_to_error():
    pytest.importorskip("textual")
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from textual.app import App, ComposeResult

    class _App(App):
        def compose(self) -> ComposeResult:
            yield ToolCallRow(call_id="c2", tool_name="read_file", args_summary="path='src/missing.py'")

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.set_result(summary="ENOENT: нет файла", error=True)
        await pilot.pause()
        content = str(row.query_one(".trace-content").render())
        prefix = str(row.query_one(".trace-prefix").render())
        assert "✗" in prefix
        assert "ENOENT" in content
        assert "-> ENOENT: нет файла" in content  # симметрично ok


@pytest.mark.asyncio
async def test_tool_call_row_error_text_is_dim():
    pytest.importorskip("textual")
    from gc_tui.palette import DIM, ERR
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from textual.app import App, ComposeResult

    class _App(App):
        def compose(self) -> ComposeResult:
            yield ToolCallRow(call_id="c2", tool_name="exec", args_summary="cmd='rm'")

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.set_result(summary="permission denied", error=True)
        await pilot.pause()
        rendered = row._render_content()
        # Сегмент с текстом ошибки покрашен DIM, не ERR.
        plain = rendered.plain
        dim_segs = [plain[s.start : s.end] for s in rendered.spans if str(s.style).lower() == DIM.lower()]
        err_segs = [plain[s.start : s.end] for s in rendered.spans if str(s.style).lower() == ERR.lower()]
        assert any("permission denied" in seg for seg in dim_segs)
        assert not any("permission denied" in seg for seg in err_segs)


@pytest.mark.asyncio
async def test_tool_call_row_brackets_in_args_not_interpreted_as_markup():
    """Args containing [brackets] must render literally (Content not str)."""
    pytest.importorskip("textual")
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from textual.app import App, ComposeResult

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
    from gc_tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="abc-123", tool_name="t", args_summary="")
    assert row.call_id == "abc-123"


@pytest.mark.asyncio
async def test_tool_call_row_ok_with_empty_summary_omits_arrow():
    pytest.importorskip("textual")
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from textual.app import App, ComposeResult

    class _App(App):
        def compose(self) -> ComposeResult:
            yield ToolCallRow(call_id="c1", tool_name="skills_load", args_summary="skill_id='x'")

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.set_result(summary="", error=False)
        await pilot.pause()
        content = str(row.query_one(".trace-content").render())
        prefix = str(row.query_one(".trace-prefix").render())
        assert "✓" in prefix
        assert "-> " not in content  # стрелки нет при пустом summary
        assert "skills_load" in content


@pytest.mark.asyncio
async def test_tool_call_row_ok_state_uses_styled_text():
    """OK: prefix содержит зелёный span с глифом ✓; content — dim args."""
    pytest.importorskip("textual")
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from rich.text import Text
    from textual.app import App, ComposeResult

    class _App(App):
        def compose(self) -> ComposeResult:
            yield ToolCallRow(call_id="c1", tool_name="read_file", args_summary="path='x.py'")

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.set_result(summary="100 строк", error=False)
        await pilot.pause()
        prefix = row._render_prefix()
        assert isinstance(prefix, Text)
        ok_color = "#5FAE7F"
        green = [prefix.plain[s.start : s.end] for s in prefix.spans if str(s.style).lower() == ok_color.lower()]
        assert any("✓" in seg for seg in green), f"spans={prefix.spans}"


@pytest.mark.asyncio
async def test_tool_call_row_tool_name_uses_tool_name_color():
    """Имя тула (без скобок) красится palette.TOOL_NAME — primary-сигнал в строке;
    скобки/args/путь — DIM (один общий тон обвеса)."""
    pytest.importorskip("textual")
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from rich.text import Text
    from textual.app import App, ComposeResult

    class _App(App):
        def compose(self) -> ComposeResult:
            yield ToolCallRow(call_id="c1", tool_name="read_file", args_summary="path='src/thread_store.py'")

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.set_result(summary="312 строк", error=False)
        await pilot.pause()
        rendered = row._render_content()
        assert isinstance(rendered, Text)
        tool_name_spans = [
            rendered.plain[s.start : s.end] for s in rendered.spans if _TOOL_NAME_HEX in str(s.style).lower()
        ]
        assert "read_file" in tool_name_spans, (
            f"No TOOL_NAME-colored span equal to 'read_file'; spans="
            f"{[(rendered.plain[s.start : s.end], str(s.style)) for s in rendered.spans]}"
        )


@pytest.mark.asyncio
async def test_tool_call_row_set_result_twice_overwrites():
    pytest.importorskip("textual")
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from textual.app import App, ComposeResult

    class _App(App):
        def compose(self) -> ComposeResult:
            yield ToolCallRow(call_id="c1", tool_name="read_file", args_summary="path='src/x.py'")

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.set_result(summary="initial", error=False)
        await pilot.pause()
        row.set_result(summary="updated", error=True)
        await pilot.pause()
        content = str(row.query_one(".trace-content").render())
        prefix = str(row.query_one(".trace-prefix").render())
        assert "updated" in content
        assert "✗" in prefix
        assert "initial" not in content
        assert "✓" not in prefix


@pytest.mark.asyncio
async def test_ctrl_click_posts_open_tool_call_detail_request():
    pytest.importorskip("textual")
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from textual import events

    captured: list = []

    from textual.app import App

    class _App(App):
        def compose(self):
            yield ToolCallRow(call_id="c42", tool_name="read_file", args_summary="", event_no=1)

        def on_open_tool_call_detail_request(self, message):
            captured.append(message.call_id)

    app = _App()
    async with app.run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        click = events.Click(
            widget=row,
            x=0,
            y=0,
            delta_x=0,
            delta_y=0,
            button=1,
            shift=False,
            meta=False,
            ctrl=True,
            chain=1,
        )
        row.on_click(click)
        await pilot.pause()

    assert captured == ["c42"]


@pytest.mark.asyncio
async def test_focus_does_not_render_event_number_prefix():
    """`[N]`-префикс на фокусе убран: он дёргал строку по горизонтали и мешал
    выделению/копированию. Фокус теперь показывается фоном (:focus background).
    Регрессия: на фокусе `[N]` не должен появляться ни до, ни после."""
    pytest.importorskip("textual")
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from textual.app import App, ComposeResult
    from textual.widgets import Static

    class _App(App):
        AUTO_FOCUS = None

        def compose(self) -> ComposeResult:
            yield ToolCallRow(call_id="c1", tool_name="read_file", args_summary="path='x'", event_no=42)
            yield Static("decoy", id="decoy")

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        content = row.query_one(".trace-content")

        assert "[42]" not in str(content.render())
        row.focus()
        await pilot.pause(0.05)
        assert row.has_focus
        # На фокусе [N] больше НЕ появляется.
        assert "[42]" not in str(content.render())


@pytest.mark.asyncio
async def test_focus_handler_has_focus_after_super():
    """Контракт: `super()._on_focus(event)` ставит has_focus=True ДО возврата.

    Anti-regression: если кто-то снова уберёт super()._on_focus(event),
    has_focus останется False внутри subclass-handler-а — этот тест упадёт.
    """
    pytest.importorskip("textual")
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from textual.app import App, ComposeResult

    captured = {"has_focus_after_super": None}

    class _ProbeRow(ToolCallRow):
        def _on_focus(self, event):
            super()._on_focus(event)
            captured["has_focus_after_super"] = self.has_focus

    class _App(App):
        def compose(self) -> ComposeResult:
            yield _ProbeRow(call_id="c1", tool_name="read_file", args_summary="path='x'", event_no=1)

    async with _App().run_test() as pilot:
        pilot.app.query_one(_ProbeRow).focus()
        await pilot.pause(0.05)

    assert captured["has_focus_after_super"] is True


def test_running_row_shows_elapsed_seconds():
    import time

    from gc_tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="c1", tool_name="subagent_planner", args_summary="task=x")
    row._start_monotonic = time.monotonic() - 47.0
    r = str(row._render_content())
    assert "· 47с" in r
    assert "running" not in r


def test_running_row_elapsed_same_for_any_tool():
    import time

    from gc_tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="c2", tool_name="read_file", args_summary="path=a")
    row._start_monotonic = time.monotonic() - 3.0
    assert "· 3с" in str(row._render_content())


def test_running_row_elapsed_minutes_format():
    import time

    from gc_tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="c1", tool_name="t", args_summary="")
    row._start_monotonic = time.monotonic() - 125  # 2м 5с
    assert "· 2м 5с" in str(row._render_content())


def test_elapsed_excludes_paused_interval(monkeypatch):
    """confirm/ask_human: время ожидания решения юзера не входит в elapsed тула."""
    from gc_tui.widgets import tool_call_row as tcr
    from gc_tui.widgets.tool_call_row import ToolCallRow

    clock = {"t": 100.0}
    monkeypatch.setattr(tcr.time, "monotonic", lambda: clock["t"])
    row = ToolCallRow(call_id="c1", tool_name="t", args_summary="")  # _start=100
    clock["t"] = 105.0
    assert row._elapsed_s() == 5
    row.pause()  # заморозка на 105
    clock["t"] = 130.0  # 25с раздумья юзера над баннером
    assert row._elapsed_s() == 5, "во время паузы elapsed заморожен"
    row.resume()
    clock["t"] = 132.0
    assert row._elapsed_s() == 7, "25с ожидания исключены (105..132 минус 25 = 7)"


def test_pause_resume_idempotent(monkeypatch):
    from gc_tui.widgets import tool_call_row as tcr
    from gc_tui.widgets.tool_call_row import ToolCallRow

    clock = {"t": 100.0}
    monkeypatch.setattr(tcr.time, "monotonic", lambda: clock["t"])
    row = ToolCallRow(call_id="c1", tool_name="t", args_summary="")
    row.resume()  # resume без pause — no-op
    clock["t"] = 110.0
    assert row._elapsed_s() == 10
    row.pause()  # заморозка на 110
    clock["t"] = 120.0
    row.pause()  # повторный pause не двигает точку заморозки
    clock["t"] = 130.0
    assert row._elapsed_s() == 10


def test_running_glyph_pulses_color():
    """▸ статичен по форме, но цвет глифа в prefix меняется между кадрами пульса."""
    from gc_tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="c1", tool_name="t", args_summary="")
    row._pulse_frame = 0
    p0 = row._render_prefix()
    row._pulse_frame = 3
    p3 = row._render_prefix()
    assert "▸" in p0.plain and "▸" in p3.plain
    assert [str(s.style) for s in p0.spans] != [str(s.style) for s in p3.spans]


def test_pulse_cycle_length_is_ten():
    """Цикл пульса ▸ = 10 кадров на 0.1с-тике (плавное дыхание ≈1.0с, 10fps):
    frame10 возвращается к frame0, frame5 (пик) внутри цикла отличается."""
    from gc_tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="c1", tool_name="t", args_summary="")

    def glyph_color(frame: int) -> str:
        row._pulse_frame = frame
        p = row._render_prefix()
        return next(str(s.style) for s in p.spans if "▸" in p.plain[s.start : s.end])

    c0 = glyph_color(0)
    assert glyph_color(10) == c0  # цикл = 10 → frame10 == frame0
    assert glyph_color(5) != c0  # пик цикла отличается


def test_pulse_tick_advances_pulse_while_running():
    """Пульс ▸ двигается на собственном 0.1с-тике (_pulse_tick), пока running."""
    from gc_tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="c1", tool_name="t", args_summary="")
    f0 = row._pulse_frame
    row._pulse_tick()
    assert row._pulse_frame == f0 + 1


def test_pulse_tick_noop_after_completion():
    """После завершения строки _pulse_tick не двигает кадр (тик гасится)."""
    from gc_tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="c1", tool_name="t", args_summary="")
    row.set_result(summary="ok", error=False)
    f = row._pulse_frame
    row._pulse_tick()
    assert row._pulse_frame == f


def test_refresh_running_elapsed_does_not_advance_pulse():
    """Секундный поллер больше НЕ двигает пульс (он на собственном тике)."""
    from gc_tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="c1", tool_name="t", args_summary="")
    f0 = row._pulse_frame
    row.refresh_running_elapsed()
    assert row._pulse_frame == f0


def test_completed_ok_row_has_no_elapsed():
    from gc_tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="c3", tool_name="subagent_planner", args_summary="x")
    row.set_result(summary="ok", error=False)
    assert "✓" in str(row._render_prefix())
    assert "running" not in str(row._render_content())


def test_completed_error_row_has_no_elapsed():
    from gc_tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="c4", tool_name="subagent_planner", args_summary="x")
    row.set_result(summary="boom", error=True)
    assert "✗" in str(row._render_prefix())
    assert "running" not in str(row._render_content())


def test_refresh_running_elapsed_noop_after_completion():
    from gc_tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="c5", tool_name="subagent_planner", args_summary="x")
    row.set_result(summary="ok", error=False)
    row.refresh_running_elapsed()  # не возвращает в running
    assert "✓" in str(row._render_prefix())


@pytest.mark.asyncio
async def test_poll_active_ticks_running_rows():
    pytest.importorskip("textual")
    import time

    from gc_tui.app import TuiApp
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from textual.containers import VerticalScroll

    class _LiveRunner:  # без живого run инвариант поллинга финализирует строку
        def is_running(self) -> bool:
            return True

    app = TuiApp()
    app.active_runner = _LiveRunner()
    async with app.run_test() as pilot:
        scr = pilot.app.screen  # TraceScreen (дефолтный экран)
        row = ToolCallRow(call_id="c1", tool_name="subagent_planner", args_summary="task=x")
        scr.query_one("#trace", VerticalScroll).mount(row)
        await pilot.pause()  # compose → _content_static готов (running · 0с)
        scr._active_calls["c1"] = row
        row._start_monotonic = time.monotonic() - 5.0
        scr._poll_active()  # должен дёрнуть refresh у running-строк
        await pilot.pause()
        assert "· 5с" in str(row._content_static.render())


# ---- mark_interrupted: терминальное состояние «прервано» (spec 2026-06-10) ----


def test_mark_interrupted_renders_dim_terminal_state():
    from gc_tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="c6", tool_name="subagent_critic", args_summary="t")
    row.mark_interrupted()
    assert "·" in str(row._render_prefix())
    content = str(row._render_content())
    assert "прервано" in content
    assert "running" not in content


def test_mark_interrupted_noop_after_result_and_idempotent():
    from gc_tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="c7", tool_name="read_file", args_summary="p")
    row.set_result(summary="312 строк", error=False)
    row.mark_interrupted()  # после результата — no-op
    assert "✓" in str(row._render_prefix())
    row2 = ToolCallRow(call_id="c8", tool_name="read_file", args_summary="p")
    row2.mark_interrupted()
    row2.mark_interrupted()  # повторный вызов — no-op
    assert "прервано" in str(row2._render_content())


def test_refresh_running_elapsed_noop_after_interrupted():
    from gc_tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="c9", tool_name="exec", args_summary="")
    row.mark_interrupted()
    row.refresh_running_elapsed()  # не возвращает в running
    content = str(row._render_content())
    assert "прервано" in content and "running" not in content
