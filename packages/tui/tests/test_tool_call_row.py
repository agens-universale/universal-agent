"""ToolCallRow — один виджет, который мутирует от running к ok/err."""

import pytest


def _shown_lines(row) -> str:
    """Смонтированные строки `└` вызова — как их видно в ленте."""
    from gc_tui.widgets.consequence_line import ConsequenceLine

    return "\n".join(ln.text.plain for ln in row.query(ConsequenceLine))


@pytest.mark.asyncio
async def test_tool_call_row_initial_state():
    pytest.importorskip("textual")
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from textual.app import App, ComposeResult

    class _App(App):
        def compose(self) -> ComposeResult:
            yield ToolCallRow(
                call_id="c1",
                label="read_file",
                target="path='src/x.py'",
            )

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        content = str(row.query_one(".trace-content").render())
        prefix = str(row.query_one(".trace-prefix").render())
        assert "read_file" in content
        assert "src/x.py" in content
        assert "●" in prefix  # running-глиф ведёт строку (prefix)
        assert "running" not in content  # слово убрано


@pytest.mark.asyncio
async def test_tool_call_row_mutate_to_ok():
    pytest.importorskip("textual")
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from textual.app import App, ComposeResult

    class _App(App):
        def compose(self) -> ComposeResult:
            yield ToolCallRow(
                call_id="c1",
                label="read_file",
                target="path='src/x.py'",
            )

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.set_result(summary="312 строк", outcome="ok")
        await pilot.pause()
        content = str(row.query_one(".trace-content").render())
        prefix = str(row.query_one(".trace-prefix").render())
        assert "●" in prefix
        assert "312 строк" in content
        assert "running" not in content


@pytest.mark.asyncio
async def test_tool_call_row_mutate_to_error():
    pytest.importorskip("textual")
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from textual.app import App, ComposeResult

    class _App(App):
        def compose(self) -> ComposeResult:
            yield ToolCallRow(
                call_id="c2",
                label="read_file",
                target="path='src/missing.py'",
            )

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.set_result(summary="ENOENT: нет файла", outcome="error")
        await pilot.pause()
        content = str(row.query_one(".trace-content").render())
        prefix = str(row.query_one(".trace-prefix").render())
        line = _shown_lines(row)
        assert "●" in prefix
        assert "ENOENT" not in content  # у ошибки хвоста нет
        assert line == "  └ ENOENT: нет файла"  # строкой под вызовом


@pytest.mark.asyncio
async def test_tool_call_row_error_text_is_err_line():
    pytest.importorskip("textual")
    from gc_tui.palette import DIM, ERR
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from textual.app import App, ComposeResult

    class _App(App):
        def compose(self) -> ComposeResult:
            yield ToolCallRow(call_id="c2", label="exec", target="cmd='rm'")

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.set_result(summary="permission denied", outcome="error")
        await pilot.pause()
        from gc_tui.widgets.consequence_line import ConsequenceLine

        rendered = row.query_one(ConsequenceLine).text
        # Текст ошибки — строкой `└` (сам `└` DIM) цветом ERR (спека P3 §3).
        plain = rendered.plain
        dim_segs = [
            plain[s.start : s.end]
            for s in rendered.spans
            if str(s.style).lower() == DIM.lower()
        ]
        err_segs = [
            plain[s.start : s.end]
            for s in rendered.spans
            if str(s.style).lower() == ERR.lower()
        ]
        assert any("└" in seg for seg in dim_segs)
        assert any("permission denied" in seg for seg in err_segs)
        assert "permission denied" not in row._render_content().plain


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
                label="search_files",
                target="pattern='[bold]x[/bold]'",
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

    row = ToolCallRow(call_id="abc-123", label="t", target="")
    assert row.call_id == "abc-123"


@pytest.mark.asyncio
async def test_tool_call_row_ok_with_empty_summary_omits_arrow():
    pytest.importorskip("textual")
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from textual.app import App, ComposeResult

    class _App(App):
        def compose(self) -> ComposeResult:
            yield ToolCallRow(
                call_id="c1",
                label="skills_load",
                target="skill_id='x'",
            )

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.set_result(summary="", outcome="ok")
        await pilot.pause()
        content = str(row.query_one(".trace-content").render())
        prefix = str(row.query_one(".trace-prefix").render())
        assert "●" in prefix
        assert " · " not in content  # хвоста нет при пустом summary
        assert "skills_load" in content


@pytest.mark.asyncio
async def test_tool_call_row_ok_state_uses_styled_text():
    """OK: prefix содержит зелёный span с глифом ●; content — dim args."""
    pytest.importorskip("textual")
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from rich.text import Text
    from textual.app import App, ComposeResult

    class _App(App):
        def compose(self) -> ComposeResult:
            yield ToolCallRow(
                call_id="c1", label="read_file", target="path='x.py'"
            )

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.set_result(summary="100 строк", outcome="ok")
        await pilot.pause()
        prefix = row._render_prefix()
        assert isinstance(prefix, Text)
        ok_color = "#5FAE7F"
        green = [
            prefix.plain[s.start : s.end]
            for s in prefix.spans
            if str(s.style).lower() == ok_color.lower()
        ]
        assert any("●" in seg for seg in green), f"spans={prefix.spans}"


@pytest.mark.parametrize("live", [True, False])
def test_single_row_word_ink_bold_object_by_state(live):
    """Одиночная строка (MCP, Skill, прочие одиночные категории, §10):
    слово `INK` жирным; объект идущей — `INK`, законченной — `SUB_INK`."""
    from gc_tui.palette import INK, SUB_INK
    from gc_tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="m1", label="Jira", target="search  KEY-1")
    if not live:
        row.set_result(summary="", outcome="ok")
    content = row._render_content()
    assert _style_at(content, "Jira") == f"bold {INK}"
    assert _style_at(content, "KEY-1") == (INK if live else SUB_INK)


@pytest.mark.asyncio
async def test_tool_call_row_set_result_twice_overwrites():
    pytest.importorskip("textual")
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from textual.app import App, ComposeResult

    class _App(App):
        def compose(self) -> ComposeResult:
            yield ToolCallRow(
                call_id="c1",
                label="read_file",
                target="path='src/x.py'",
            )

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.set_result(summary="initial", outcome="ok")
        await pilot.pause()
        row.set_result(summary="updated", outcome="error")
        await pilot.pause()
        content = str(row.query_one(".trace-content").render())
        prefix = str(row.query_one(".trace-prefix").render())
        line = _shown_lines(row)
        assert "updated" in line
        assert "●" in prefix
        assert "initial" not in content


@pytest.mark.asyncio
async def test_ctrl_click_posts_open_tool_call_detail_request():
    pytest.importorskip("textual")
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from textual import events

    captured: list = []

    from textual.app import App

    class _App(App):
        def compose(self):
            yield ToolCallRow(
                call_id="c42",
                label="read_file",
                target="",
                event_no=1,
            )

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
            yield ToolCallRow(
                call_id="c1",
                label="read_file",
                target="path='x'",
                event_no=42,
            )
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
            yield _ProbeRow(
                call_id="c1",
                label="Read",
                target="x",
                event_no=1,
            )

    async with _App().run_test() as pilot:
        pilot.app.query_one(_ProbeRow).focus()
        await pilot.pause(0.05)

    assert captured["has_focus_after_super"] is True


def test_running_row_shows_elapsed_seconds():
    import time

    from gc_tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="c1", label="subagent_planner", target="task=x")
    row._start_monotonic = time.monotonic() - 47.0
    r = str(row._render_content())
    assert "· 47с" in r
    assert "running" not in r


def test_running_row_elapsed_same_for_any_tool():
    import time

    from gc_tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="c2", label="read_file", target="path=a")
    row._start_monotonic = time.monotonic() - 3.0
    assert "· 3с" in str(row._render_content())


def test_running_row_elapsed_minutes_format():
    import time

    from gc_tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="c1", label="t", target="")
    row._start_monotonic = time.monotonic() - 125  # 2 м 5 с
    assert "· 2м 5с" in str(row._render_content())


def test_elapsed_excludes_paused_interval(monkeypatch):
    """confirm/ask_human: время ожидания решения юзера не входит в elapsed тула."""
    from gc_tui.widgets import tool_call_row as tcr
    from gc_tui.widgets.tool_call_row import ToolCallRow

    clock = {"t": 100.0}
    monkeypatch.setattr(tcr.time, "monotonic", lambda: clock["t"])
    row = ToolCallRow(call_id="c1", label="t", target="")  # _start=100
    clock["t"] = 105.0
    assert row._elapsed_s() == 5
    row.pause()  # заморозка на 105
    clock["t"] = 130.0  # 25с раздумья юзера над баннером
    assert row._elapsed_s() == 5, "во время паузы elapsed заморожен"
    row.resume()
    clock["t"] = 132.0
    assert row._elapsed_s() == 7, (
        "25с ожидания исключены (105..132 минус 25 = 7)"
    )


def test_pause_resume_idempotent(monkeypatch):
    from gc_tui.widgets import tool_call_row as tcr
    from gc_tui.widgets.tool_call_row import ToolCallRow

    clock = {"t": 100.0}
    monkeypatch.setattr(tcr.time, "monotonic", lambda: clock["t"])
    row = ToolCallRow(call_id="c1", label="t", target="")
    row.resume()  # resume без pause — no-op
    clock["t"] = 110.0
    assert row._elapsed_s() == 10
    row.pause()  # заморозка на 110
    clock["t"] = 120.0
    row.pause()  # повторный pause не двигает точку заморозки
    clock["t"] = 130.0
    assert row._elapsed_s() == 10


def _glyph_color(row, frame: int) -> str:
    row._pulse_frame = frame
    p = row._render_prefix()
    return next(
        str(s.style) for s in p.spans if "●" in p.plain[s.start : s.end]
    )


def _glyph_at(row, frame: int) -> tuple[str, int]:
    """Клетка глифа в кадре мигания и ширина префикса."""
    from rich.cells import cell_len

    row._pulse_frame = frame
    p = row._render_prefix()
    return p.plain.rstrip(" ")[-1:] or " ", cell_len(p.plain)


def test_running_glyph_blinks_by_disappearing():
    """● мигает исчезая: в кадре «вкл» — ● цветом RUN_COLOR, в кадре «выкл»
    клетка пустая (не серый кружок); ширина префикса не меняется."""
    from gc_tui.palette import RUN_COLOR
    from gc_tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="c1", label="t", target="")
    assert _glyph_color(row, 0) == RUN_COLOR
    on_glyph, on_width = _glyph_at(row, 0)
    off_glyph, off_width = _glyph_at(row, 8)
    assert (on_glyph, off_glyph) == ("●", " ")
    assert on_width == off_width


def test_blink_rhythm_visible_500ms_hidden_700ms():
    """Ритм на 0.1с-тике (решение владельца): 0.5с виден, 0.7с скрыт."""
    from gc_tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="c1", label="t", target="")
    seen = [_glyph_at(row, f)[0] for f in range(24)]
    assert seen == (["●"] * 5 + [" "] * 7) * 2


def test_trace_timer_tail_has_no_space_before_unit():
    """Хвост идущего вызова — слитно, как в индикаторе хода: ` · 2с`."""
    from gc_tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="c1", label="t", target="")
    row._elapsed_s = lambda: 2
    assert row._render_content().plain.endswith("· 2с")


@pytest.mark.parametrize(
    "seconds, tail",
    [(0.0, ""), (0.9, ""), (1.0, " · 1с"), (72.0, " · 1м 12с")],
)
def test_frozen_timer_only_from_one_second(seconds, tail):
    """Застывший таймер — только у вызова от 1 с; быстрее — без таймера,
    без «· 0с» (владелец 2026-10-23)."""
    from gc_tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="c1", label="Read", target="a.py")
    row.set_result(summary="", outcome="ok")
    row.set_elapsed(seconds)
    assert row._render_content().plain == f"Read  a.py{tail}"


@pytest.mark.parametrize("seconds, tail", [(0, ""), (1, " · 1с")])
def test_live_timer_only_from_one_second(seconds, tail):
    """Идущий вызов: до целой секунды — без таймера, только дыхание `●`
    (владелец 2026-10-26)."""
    from gc_tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="c1", label="Read", target="a.py")
    row._elapsed_s = lambda: seconds
    assert row._render_content().plain == f"Read  a.py{tail}"


def test_pulse_tick_advances_pulse_while_running():
    """Пульс ▸ двигается на собственном 0.1с-тике (_pulse_tick), пока running."""
    from gc_tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="c1", label="t", target="")
    f0 = row._pulse_frame
    row._pulse_tick()
    assert row._pulse_frame == f0 + 1


def test_pulse_tick_noop_after_completion():
    """После завершения строки _pulse_tick не двигает кадр (тик гасится)."""
    from gc_tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="c1", label="t", target="")
    row.set_result(summary="ok", outcome="ok")
    f = row._pulse_frame
    row._pulse_tick()
    assert row._pulse_frame == f


def test_refresh_running_elapsed_does_not_advance_pulse():
    """Секундный поллер больше НЕ двигает пульс (он на собственном тике)."""
    from gc_tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="c1", label="t", target="")
    f0 = row._pulse_frame
    row.refresh_running_elapsed()
    assert row._pulse_frame == f0


def test_completed_ok_row_has_no_elapsed():
    from gc_tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="c3", label="subagent_planner", target="x")
    row.set_result(summary="ok", outcome="ok")
    assert "●" in str(row._render_prefix())
    assert "running" not in str(row._render_content())


def test_completed_error_row_has_no_elapsed():
    from gc_tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="c4", label="subagent_planner", target="x")
    row.set_result(summary="boom", outcome="error")
    assert "●" in str(row._render_prefix())
    assert "running" not in str(row._render_content())


def test_refresh_running_elapsed_noop_after_completion():
    from gc_tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="c5", label="subagent_planner", target="x")
    row.set_result(summary="ok", outcome="ok")
    row.refresh_running_elapsed()  # не возвращает в running
    assert "●" in str(row._render_prefix())


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
        row = ToolCallRow(
            call_id="c1", label="subagent_planner", target="task=x"
        )
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

    row = ToolCallRow(call_id="c6", label="subagent_critic", target="t")
    row.mark_interrupted()
    assert "○" in str(row._render_prefix())
    content = str(row._render_content())
    assert "прервано" in content
    assert "running" not in content


def test_mark_interrupted_noop_after_result_and_idempotent():
    from gc_tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="c7", label="read_file", target="p")
    row.set_result(summary="312 строк", outcome="ok")
    row.mark_interrupted()  # после результата — no-op
    assert "●" in str(row._render_prefix())
    row2 = ToolCallRow(call_id="c8", label="read_file", target="p")
    row2.mark_interrupted()
    row2.mark_interrupted()  # повторный вызов — no-op
    assert "прервано" in str(row2._render_content())


def test_refresh_running_elapsed_noop_after_interrupted():
    from gc_tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="c9", label="exec", target="")
    row.mark_interrupted()
    row.refresh_running_elapsed()  # не возвращает в running
    content = str(row._render_content())
    assert "прервано" in content and "running" not in content


# ---- счётчики правки и кнопка ❐ (спека P3 §10, решение плана 1) ----


def _edit_row():
    from gc_tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="e1", label="Edit", target="a.py")
    row.set_result(summary="ok", outcome="ok")
    return row


def test_edit_counts_follow_the_object():
    """` (+a −r)` — сразу после объекта, до хвоста результата; `+a` — OK,
    `−r` — ERR, скобки — INK, как объект слева (спека P4 §10)."""
    from gc_tui.palette import ERR, INK, OK

    row = _edit_row()
    row.set_edit_counts(12, 3, copy_text="diff")
    content = row._render_content()
    assert content.plain == "Edit  a.py (+12 −3) · ok"
    styles = {
        content.plain[s.start : s.end]: str(s.style) for s in content.spans
    }
    assert styles["+12"] == OK
    assert styles["−3"] == ERR
    assert styles[" ("] == INK
    assert styles[")"] == INK


@pytest.mark.parametrize(
    "add, removed, tail",
    [
        (4, 2, " (+4 −2)"),
        (37, 0, " (+37 −0)"),
        (0, 3, " (+0 −3)"),
        (0, 0, " (+0 −0)"),
    ],
)
def test_edit_counts_keep_zero_part(add, removed, tail):
    """Счётчики всегда в обе части, нулевая тоже (владелец 2026-10-23)."""
    row = _edit_row()
    row.set_edit_counts(add, removed, copy_text="diff")
    assert row._render_content().plain == f"Edit  a.py{tail} · ok"


def test_row_without_edit_has_no_counts():
    assert "+" not in _edit_row()._render_content().plain


class _RowHost:
    """Хост строки со стилями приложения и захватом запросов копирования."""

    @staticmethod
    def make(row):
        from textual.app import App, ComposeResult

        from gc_testkit.paths import TUI_PKG
        from gc_tui import palette
        from gc_tui.messages import CopyBlockRequest

        class _Host(App):
            CSS_PATH = str(TUI_PKG / "styles.tcss")

            def get_css_variables(self) -> dict:
                variables = super().get_css_variables()
                variables.update(palette.css_variables())
                return variables

            def __init__(self) -> None:
                super().__init__()
                self.copied: list[str] = []

            def compose(self) -> ComposeResult:
                yield row

            def on_copy_block_request(self, event: CopyBlockRequest) -> None:
                self.copied.append(event.payload)

        return _Host()


@pytest.mark.asyncio
async def test_copy_button_hidden_until_edit_counts():
    """❐ есть только у строки с правкой: появляется вместе со счётчиками."""
    from textual.widgets import Static

    row = _edit_row()
    app = _RowHost.make(row)
    async with app.run_test(size=(80, 5)) as pilot:
        button = row.query_one(".block-copy", Static)
        assert not button.display
        row.set_edit_counts(1, 1, copy_text="diff")
        await pilot.pause()
        assert button.display
        assert button.region.right == row.region.right
        assert button.region.y == row.region.y


@pytest.mark.asyncio
async def test_no_copy_button_without_diff_text():
    """Нечего копировать — нет ❐; счётчики при этом видны."""
    from textual.widgets import Static

    row = _edit_row()
    app = _RowHost.make(row)
    async with app.run_test(size=(80, 5)) as pilot:
        row.set_edit_counts(2, 0, copy_text="")
        await pilot.pause()
        assert not row.query_one(".block-copy", Static).display
        assert "(+2 −0)" in row._render_content().plain


@pytest.mark.asyncio
async def test_copy_button_copies_diff_and_flashes():
    """Клик по ❐ шлёт весь дифф и на время показывает ✓."""
    from textual.widgets import Static

    from gc_tui.widgets.block_panel import DONE_GLYPH, copy_glyph

    row = _edit_row()
    row.set_edit_counts(1, 1, copy_text="--- a/a.py\n+++ b/a.py\n")
    app = _RowHost.make(row)
    async with app.run_test(size=(80, 5)) as pilot:
        button = row.query_one(".block-copy", Static)
        assert button.visual.plain == copy_glyph()
        await pilot.click(".block-copy")
        await pilot.pause()
        assert app.copied == ["--- a/a.py\n+++ b/a.py\n"]
        assert button.visual.plain == DONE_GLYPH


@pytest.mark.asyncio
async def test_copy_button_excluded_from_selection_and_focus_click():
    """❐ — служебный элемент: вне выделения текста и фокус по клику не
    берёт (экран ленты спрашивает `focus_on_click` у него, а не у строки)."""
    row = _edit_row()
    app = _RowHost.make(row)
    async with app.run_test(size=(80, 5)):
        button = row.query_one(".block-copy")
        assert type(button).ALLOW_SELECT is False
        assert button.get_selection(None) is None
        assert button.focus_on_click() is False


@pytest.mark.asyncio
async def test_copy_button_keeps_hover_canon(tui_app_factory):
    """Наведение на ❐ красит его акцентом — канон `.block-copy:hover`."""
    from textual.widgets import Static

    from gc_tui import palette

    row = _edit_row()
    row.set_edit_counts(1, 1, copy_text="diff")
    app = tui_app_factory(row)
    async with app.run_test(size=(80, 5)) as pilot:
        await pilot.hover(".block-copy")
        await pilot.pause()
        assert (
            app.query_one(".block-copy", Static).styles.color.hex
            == palette.ACCENT.upper()
        )


# ---- слово строки: роль задаёт вид (P4 §10) ----


def _style_at(content, fragment: str) -> str:
    """Стиль спана, на который приходится начало ``fragment``."""
    at = content.plain.index(fragment)
    (span,) = [s for s in content.spans if s.start <= at < s.end]
    return str(span.style)


def test_command_word_role_decides_look():
    """Слово команды — явная роль: `Ran  команда` через два пробела, слово
    `INK` жирным, законченная команда `SUB_INK`."""
    from gc_tui.palette import INK, SUB_INK
    from gc_tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="x1", label="Run", target="make docs")
    row.set_result(summary="", outcome="ok")
    row.set_word("Ran", role="command")
    content = row._render_content()
    assert content.plain.startswith("Ran  make docs")
    assert _style_at(content, "Ran") == f"bold {INK}"
    assert _style_at(content, "make docs") == SUB_INK


def test_single_edit_word_ink_bold_path_ink():
    """Одиночная правка (§6.2, §10): слово `INK` жирным через пробел,
    путь `INK` — и после конца."""
    from gc_tui.palette import INK
    from gc_tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="e1", label="Edit", target="a.py")
    row.set_result(summary="", outcome="ok")
    row.set_word("Edited", role="edit")
    content = row._render_content()
    assert content.plain.startswith("Edited a.py")
    assert _style_at(content, "Edited") == f"bold {INK}"
    assert _style_at(content, "a.py") == INK


def test_agent_word_role_name_is_ink():
    """`Agent`: слово `INK` жирным, имя субагента `INK` и после конца."""
    from gc_tui.palette import INK
    from gc_tui.widgets.tool_call_row import ToolCallRow

    row = ToolCallRow(call_id="a1", label="Agent", target="worker")
    row.set_result(summary="", outcome="ok")
    row.set_word("Agent", role="agent")
    content = row._render_content()
    assert content.plain.startswith("Agent  worker")
    assert _style_at(content, "Agent") == f"bold {INK}"
    assert _style_at(content, "worker") == INK
