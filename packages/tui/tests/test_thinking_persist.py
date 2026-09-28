import pytest


@pytest.mark.asyncio
async def test_indicator_persists_done_after_completed(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp(
        threads_dir=str(tmp_path / "threads")
    ).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task("t-1")
        await pilot.pause()
        # смонтировать индикатор (как во время прогона) и пометить задачу completed
        scr._show_thinking_indicator(state="thinking")
        await pilot.pause()
        scr._store.update_tui_meta("t-1", {"status": "completed"})
        # эмуляция idle-тика poll: not running → ветка done-persist
        scr._poll_active()
        await pilot.pause()
        ind = scr._thinking
        assert ind is not None, "индикатор не должен уничтожаться на completed"
        assert ind._state == "done"
