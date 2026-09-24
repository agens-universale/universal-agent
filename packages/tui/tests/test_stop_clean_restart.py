"""TraceScreen._cmd_stop — индикатор → «■ Отменено» + replace runner для clean restart."""

import pytest


@pytest.mark.asyncio
async def test_cmd_stop_sets_indicator_cancelled():
    """После _cmd_stop (при живом индикаторе) индикатор переходит в состояние
    «cancelled» («■ Отменено») — отдельная [stopped]-строка в трейсе не добавляется."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    class _FakeRunner:
        def is_running(self) -> bool:
            return True

        def stop(self) -> None:
            pass

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        scr._events_since_last_user_send = 5  # не cancel-with-undo
        pilot.app.active_runner = _FakeRunner()
        # Смонтировать индикатор
        scr._show_thinking_indicator()
        await pilot.pause()
        assert len(scr.query(ThinkingIndicator)) == 1

        scr._cmd_stop()
        await pilot.pause()
        # Индикатор остаётся, но в терминальном состоянии «cancelled».
        assert len(scr.query(ThinkingIndicator)) == 1
        ind = scr.query_one(ThinkingIndicator)
        assert ind._state == "cancelled"


@pytest.mark.asyncio
async def test_cmd_stop_no_stopped_trace_row_when_run_cancelled():
    """При отмене активного прогона отдельная инфо-строка в трейс НЕ
    добавляется (статус «Отменено» виден на строке индикатора)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.trace_row import TraceRow

    class _FakeRunner:
        def is_running(self) -> bool:
            return True

        def stop(self) -> None:
            pass

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        scr._events_since_last_user_send = 5
        pilot.app.active_runner = _FakeRunner()
        scr._cmd_stop()
        await pilot.pause()
        # Информационных «·»-строк не должно быть (кроме «нет активного запуска»
        # и «confirm отменён» — но их здесь нет, runner был живой).
        meta_rows = [
            r for r in scr.query(TraceRow) if r.has_class("line-meta")
        ]
        assert not any(
            "·" in str(r.query_one(".trace-prefix").render())
            for r in meta_rows
        )


@pytest.mark.asyncio
async def test_cmd_stop_marks_runner_stopped_and_clears_active():
    """После stop active_runner=None — следующий get_runner создаст свежий."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    class _FakeRunner:
        def is_running(self) -> bool:
            return True

        def stop(self) -> None:
            pass

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        scr._events_since_last_user_send = 5
        fake = _FakeRunner()
        pilot.app.active_runner = fake
        scr._cmd_stop()
        await pilot.pause()
        assert pilot.app.active_runner is None
        assert getattr(fake, "_marked_stopped", False) is True


@pytest.mark.asyncio
async def test_cmd_stop_no_active_runner_renders_meta():
    """_cmd_stop когда runner не активен → «· нет активного запуска» (без [stopped])."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.trace_row import TraceRow

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        pilot.app.active_runner = None
        scr._cmd_stop()
        await pilot.pause()
        meta_rows = [
            r for r in scr.query(TraceRow) if r.has_class("line-meta")
        ]
        row = next(
            r
            for r in meta_rows
            if "нет активного запуска"
            in str(r.query_one(".trace-content").render())
        )
        prefix = str(row.query_one(".trace-prefix").render())
        assert "·" in prefix and "[stopped]" not in prefix
