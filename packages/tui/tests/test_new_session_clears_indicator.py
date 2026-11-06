"""После падения рана /new (empty-state) не должен оставлять/репейнтить
статус-индикатор. Основной фикс — сброс app.last_runner_error в empty-state;
гейт-инвариант — poll без активной задачи вообще не рисует индикатор."""

import pytest


@pytest.mark.asyncio
async def test_new_session_clears_error_indicator(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.turn_line import TurnLine

    async with TuiApp(
        threads_dir=str(tmp_path / "threads")
    ).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task("t-err")
        await pilot.pause()
        # эмуляция упавшего рана: ход запущен отсюда, глобальный
        # error-флаг → спадающий фронт ставит строку «✕ Ошибка» в ленте
        # (S10 §3.7), индикатора и бара нет
        scr._run_pending = True
        pilot.app.last_runner_error = "runtime: boom"
        scr._poll_active()
        await pilot.pause()
        assert scr._thinking is None
        assert [line.word for line in scr.query(TurnLine)] == ["failed"]

        # /new при не-running ране → empty-state
        scr._cmd_new()
        await pilot.pause()
        # основной фикс: терминальный error-флаг сброшен
        assert pilot.app.last_runner_error is None
        # индикатор ушёл и НЕ возвращается на следующем тике poll
        scr._poll_active()
        await pilot.pause()
        assert scr._active_task_id is None
        assert scr._thinking is None
        assert list(scr.query(TurnLine)) == []
        assert not scr.query_one("#indicator-bar").display


@pytest.mark.asyncio
async def test_poll_paints_no_indicator_without_active_task(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp(
        threads_dir=str(tmp_path / "threads")
    ).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task("t-1")
        await pilot.pause()
        # гейт-инвариант: нет активной задачи, но протухший error-флаг —
        # индикатор всё равно не должен рисоваться
        scr._active_task_id = None
        pilot.app.last_runner_error = "stale boom"
        scr._poll_active()
        await pilot.pause()
        assert scr._thinking is None
