"""Таймер дыхания ToolCallRow: есть только у живой строки."""

import pytest
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
