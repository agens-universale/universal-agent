from gc_testkit.tui_support import tui_settings
import pytest


@pytest.mark.asyncio
async def test_skills_cache_invalidated_on_run_end(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"model": "M"}),
    ).run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [{"id": "old"}]  # «старый» снимок
        scr._was_running = True  # был прогон
        scr.app.active_runner = None  # runner завершился → is_running False
        scr._poll_active()
        await pilot.pause()
        assert scr._skills_cache is None  # сброшен на спадающем фронте


@pytest.mark.asyncio
async def test_skills_cache_kept_while_idle(tmp_path):
    """Без спадающего фронта (не было прогона) кэш не сбрасывается."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"model": "M"}),
    ).run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [{"id": "old"}]
        scr._was_running = False  # прогона не было
        scr.app.active_runner = None
        scr._poll_active()
        await pilot.pause()
        assert scr._skills_cache == [{"id": "old"}]  # не тронут
