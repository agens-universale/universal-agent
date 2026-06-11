"""TraceScreen._cmd_stop — immediate destroy + replace runner для clean restart."""
import pytest


@pytest.mark.asyncio
async def test_cmd_stop_destroys_indicator_immediately():
    """После _cmd_stop индикатор удалён сразу (нет 'останавливаюсь…' фазы)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.thinking_indicator import ThinkingIndicator

    class _FakeRunner:
        def is_running(self) -> bool: return True
        def stop(self) -> None: pass

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
        assert len(scr.query(ThinkingIndicator)) == 0


@pytest.mark.asyncio
async def test_cmd_stop_renders_stopped_trace_row():
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.trace_row import TraceRow

    class _FakeRunner:
        def is_running(self) -> bool: return True
        def stop(self) -> None: pass

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        scr._events_since_last_user_send = 5
        pilot.app.active_runner = _FakeRunner()
        scr._cmd_stop()
        await pilot.pause()
        # Должна появиться [stopped] строка
        meta_rows = [r for r in scr.query(TraceRow) if r.has_class("line-meta")]
        assert any("[stopped]" in str(r.query_one(".trace-prefix").render()) for r in meta_rows)


@pytest.mark.asyncio
async def test_cmd_stop_marks_runner_stopped_and_clears_active():
    """После stop active_runner=None — следующий get_runner создаст свежий."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    class _FakeRunner:
        def is_running(self) -> bool: return True
        def stop(self) -> None: pass

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
    """_cmd_stop когда runner не активен → [stopped] нет активного запуска."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.trace_row import TraceRow

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        pilot.app.active_runner = None
        scr._cmd_stop()
        await pilot.pause()
        meta_rows = [r for r in scr.query(TraceRow) if r.has_class("line-meta")]
        assert any("нет активного запуска" in str(r.query_one(".trace-content").render())
                   for r in meta_rows)
