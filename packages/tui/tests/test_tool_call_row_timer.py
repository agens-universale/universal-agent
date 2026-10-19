"""Таймер дыхания ToolCallRow: есть только у живой строки."""

from types import SimpleNamespace

import pytest
from gc_tui.palette import RUN_COLOR
from gc_tui.widgets import tool_call_row
from gc_tui.widgets.tool_call_row import ToolCallRow
from textual.app import App, ComposeResult


def _make_app(row_factory=None):
    class _App(App):
        def compose(self) -> ComposeResult:
            if row_factory is not None:
                yield row_factory()
            else:
                yield ToolCallRow(call_id="c1", tool_name="t", args_summary="")

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
        row.set_result(summary="ok", error=False)
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
        row = ToolCallRow(call_id="c1", tool_name="t", args_summary="")
        row.set_result(summary="ok", error=False)
        return row

    async with _make_app(factory).run_test() as pilot:
        await pilot.pause()
        row = pilot.app.query_one(ToolCallRow)
        assert row._pulse_timer is None


@pytest.mark.asyncio
async def test_waiting_row_has_no_timer():
    def factory():
        return ToolCallRow(
            call_id="c1", tool_name="t", args_summary="", waiting=True
        )

    async with _make_app(factory).run_test() as pilot:
        await pilot.pause()
        row = pilot.app.query_one(ToolCallRow)
        assert row._pulse_timer is None


@pytest.mark.asyncio
async def test_set_active_starts_timer():
    def factory():
        return ToolCallRow(
            call_id="c1", tool_name="t", args_summary="", waiting=True
        )

    async with _make_app(factory).run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.set_active()
        await pilot.pause()
        assert row._pulse_timer is not None


@pytest.mark.asyncio
async def test_set_waiting_stops_timer():
    async with _make_app().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.set_waiting()
        await pilot.pause()
        assert row._pulse_timer is None


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


@pytest.mark.asyncio
async def test_pause_shows_pause_glyph_and_resume_restores():
    async with _make_app().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.pause()
        await pilot.pause()
        prefix = row._prefix_static.content
        assert "‖" in prefix.plain
        assert "▸" not in prefix.plain
        at = prefix.plain.index("‖")
        glyph_styles = [
            str(sp.style) for sp in prefix.spans if sp.start <= at < sp.end
        ]
        assert glyph_styles == [RUN_COLOR]
        row.resume()
        await pilot.pause()
        assert "▸" in row._prefix_static.content.plain


@pytest.mark.asyncio
async def test_waiting_then_active_keeps_timer():
    async with _make_app().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.set_waiting()
        row.set_active()
        await pilot.pause()
        assert row._pulse_timer is not None


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
        return _Counting(call_id="c1", tool_name="t", args_summary="")

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
