"""TraceScreen._cmd_stop — индикатор → «■ Отменено» + replace runner для clean restart."""

import json

import pytest


@pytest.mark.asyncio
async def test_cmd_stop_of_ended_run_drops_indicator(tmp_path):
    """После _cmd_stop хода, который тут же кончился, индикатора нет: исход
    — строкой итога в ленте (S10 §3.3), не состоянием индикатора."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    class _FakeRunner:
        running = True

        def is_running(self) -> bool:
            return self.running

        def stop(self) -> None:
            self.running = False  # ход кончился сразу: итог ровный

    async with TuiApp(threads_dir=str(tmp_path)).run_test() as pilot:
        scr = pilot.app.screen
        # Ход открытого треда: строка итога встаёт в его ленте.
        tid = "stop0000000000000000000000000001"
        (tmp_path / f"meta_cli_{tid}.json").write_text(
            json.dumps({"thread_id": tid}), encoding="utf-8"
        )
        (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")
        scr.attach_active_task(tid, is_resume=True)
        pilot.app.active_runner = _FakeRunner()
        scr._prepare_for_new_run()
        # Смонтировать индикатор
        scr._show_thinking_indicator()
        await pilot.pause()
        assert len(scr.query(ThinkingIndicator)) == 1

        scr._cmd_stop()
        await pilot.pause()
        assert len(scr.query(ThinkingIndicator)) == 0
        from gc_tui.widgets.turn_line import TurnLine

        (line,) = scr.query_one("#trace").query(TurnLine)
        assert line.text.plain == "■ Отменено"
        assert line.display


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
