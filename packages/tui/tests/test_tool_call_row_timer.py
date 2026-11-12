"""Таймер дыхания ToolCallRow: есть только у живой строки."""

from types import SimpleNamespace

import pytest
from arrange_counter import count_arranges
from gc_tui import textual_compat
from gc_tui.palette import RUN_COLOR
from gc_tui.widgets import tool_call_row
from gc_tui.widgets.tool_call_row import ToolCallRow
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static


# Колонки строки — как в `styles.tcss`: без них колонка текста нулевой
# ширины и не видна.
_TRACE_CSS = """
.trace-prefix { width: auto; height: auto; }
.trace-content { width: 1fr; height: auto; }
"""


def _make_app(row_factory=None):
    class _App(App):
        CSS = _TRACE_CSS

        def compose(self) -> ComposeResult:
            if row_factory is not None:
                yield row_factory()
            else:
                yield ToolCallRow(call_id="c1", label="t", target="")

    return _App()


@pytest.mark.asyncio
async def test_live_row_has_timer():
    async with _make_app().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        assert row._pulse_timer is not None


@pytest.mark.asyncio
async def test_result_stops_timer():
    async with _make_app().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.set_result(summary="ok", outcome="ok")
        await pilot.pause()
        assert row._pulse_timer is None


@pytest.mark.asyncio
async def test_interrupt_stops_timer():
    async with _make_app().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.mark_interrupted()
        await pilot.pause()
        assert row._pulse_timer is None


@pytest.mark.asyncio
async def test_finished_before_mount_has_no_timer():
    def factory():
        row = ToolCallRow(call_id="c1", label="t", target="")
        row.set_result(summary="ok", outcome="ok")
        return row

    async with _make_app(factory).run_test() as pilot:
        await pilot.pause()
        row = pilot.app.query_one(ToolCallRow)
        assert row._pulse_timer is None


@pytest.mark.asyncio
async def test_waiting_row_has_no_timer():
    def factory():
        return ToolCallRow(call_id="c1", label="t", target="", waiting=True)

    async with _make_app(factory).run_test() as pilot:
        await pilot.pause()
        row = pilot.app.query_one(ToolCallRow)
        assert row._pulse_timer is None


@pytest.mark.asyncio
async def test_set_active_starts_timer():
    def factory():
        return ToolCallRow(call_id="c1", label="t", target="", waiting=True)

    async with _make_app(factory).run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.set_active()
        await pilot.pause()
        assert row._pulse_timer is not None


@pytest.mark.asyncio
async def test_pause_stops_timer_and_resume_restarts():
    async with _make_app().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.pause()
        await pilot.pause()
        assert row._pulse_timer is None
        row.resume()
        await pilot.pause()
        assert row._pulse_timer is not None


def _glyph_styles(prefix, glyph: str) -> list[str]:
    at = prefix.plain.index(glyph)
    return [str(sp.style) for sp in prefix.spans if sp.start <= at < sp.end]


@pytest.mark.asyncio
async def test_pause_shows_static_run_dot_and_resume_restores():
    """Человеко-ожидание: статичный янтарный `●` (не `‖`), без дыхания;
    таймер строки застыл, хвоста ожидания нет — строка не спрашивала."""
    async with _make_app().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row._elapsed_s = lambda until=None: 3
        row.pause()
        await pilot.pause()
        prefix = row._prefix_static.content
        assert "‖" not in prefix.plain
        assert _glyph_styles(prefix, "●") == [RUN_COLOR]
        assert row._pulse_timer is None
        assert row._render_content().plain == "t · 3с"
        row.resume()
        await pilot.pause()
        assert row._pulse_timer is not None


@pytest.mark.asyncio
async def test_asking_row_tail_replaces_timer():
    """Строка, спросившая разрешения: на месте таймера — `· ждёт
    разрешения` цветом живого таймера; ответ «да» — таймер и дыхание."""
    async with _make_app().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row._elapsed_s = lambda until=None: 3
        row.pause(asking=True)
        await pilot.pause()
        content = row._render_content()
        assert content.plain == "t · ждёт разрешения"
        at = content.plain.index("ждёт")
        styles = [str(s.style) for s in content.spans if s.start <= at]
        assert styles[-1] == RUN_COLOR
        assert _glyph_styles(row._prefix_static.content, "●") == [RUN_COLOR]
        row.resume()
        await pilot.pause()
        assert row._render_content().plain == "t · 3с"
        assert row._pulse_timer is not None


@pytest.mark.asyncio
async def test_asking_row_refused_gives_undone_glyph():
    """Отказ после вопроса — `○` и `└` причины, хвоста ожидания нет."""
    async with _make_app().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.pause(asking=True)
        row.set_result(summary="запрещено", outcome="refused")
        await pilot.pause()
        assert "○" in row._prefix_static.content.plain
        assert "ждёт" not in row._render_content().plain


@pytest.mark.asyncio
async def test_live_row_ticks_coarse():
    class _Counting(ToolCallRow):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.ticks = 0

        def _pulse_tick(self) -> None:
            self.ticks += 1
            super()._pulse_tick()

    def factory():
        return _Counting(call_id="c1", label="t", target="")

    async with _make_app(factory).run_test() as pilot:
        row = pilot.app.query_one(_Counting)
        await pilot.pause(0.5)
        assert row.ticks >= 1


@pytest.mark.asyncio
async def test_pulse_frame_does_not_relayout():
    async with _make_app().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        calls = []
        original = row._prefix_static.update

        def spy(content, *, layout=True):
            calls.append(layout)
            original(content, layout=layout)

        row._prefix_static.update = spy
        for _ in range(3):
            row._pulse_tick()
        assert calls
        assert all(layout is False for layout in calls)


@pytest.mark.asyncio
async def test_elapsed_updates_content_only_on_change(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(
        tool_call_row,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0]),
    )
    async with _make_app().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.refresh_running_elapsed()
        first = row._content_static.content
        row.refresh_running_elapsed()
        assert row._content_static.content is first
        clock[0] += 1.0
        row.refresh_running_elapsed()
        assert row._content_static.content is not first


class _Scrolled(App):
    """Живая строка вызова первой в длинной прокрутке; после
    `scroll_end` она за экраном."""

    CSS = _TRACE_CSS

    def __init__(self, row: ToolCallRow) -> None:
        super().__init__()
        self.row = row

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="host"):
            yield self.row
            for i in range(200):
                yield Static(f"строка {i}")


def _row_with_lines() -> ToolCallRow:
    row = ToolCallRow(
        call_id="c1", label="Run", target="tests/very/long/target.py"
    )
    for i in range(3):
        row.add_consequence(Text(f"пометка {i}"))
    return row


async def _scroll(pilot, y: float | None = None) -> None:
    host = pilot.app.query_one("#host", VerticalScroll)
    if y is None:
        host.scroll_end(animate=False)
    else:
        host.scroll_to(y=y, animate=False)
    await pilot.pause()


def _tick(row: ToolCallRow, times: int = 20) -> None:
    for _ in range(times):
        row._pulse_tick()
        row.refresh_running_elapsed()


@pytest.mark.asyncio
async def test_offscreen_row_ticks_no_full_map():
    app = _Scrolled(_row_with_lines())
    async with app.run_test(size=(60, 20)) as pilot:
        await _scroll(pilot)
        with count_arranges() as counts:
            _tick(app.row)
        with count_arranges() as control:
            app.row.is_on_screen  # карту пересобирает
        assert (counts.full_map, control.full_map) == (0, 1)
        assert app.row._frame_missed is True


@pytest.mark.asyncio
async def test_partially_visible_row_no_full_map():
    """Глиф и подпись выше края, видны только строки `└`."""
    app = _Scrolled(_row_with_lines())
    async with app.run_test(size=(60, 20)) as pilot:
        await _scroll(pilot, 1)
        assert textual_compat.is_visible(app.row) is True
        assert textual_compat.is_visible(app.row._prefix_static) is False
        with count_arranges() as counts:
            _tick(app.row)
        assert counts.full_map == 0
        assert app.row._frame_missed is True


@pytest.mark.asyncio
async def test_visible_row_ticks_draw():
    app = _Scrolled(_row_with_lines())
    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        before = app.row._prefix_static.content
        app.row._pulse_tick()
        assert app.row._prefix_static.content is not before
