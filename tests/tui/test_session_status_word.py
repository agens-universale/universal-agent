import pytest


@pytest.mark.asyncio
async def test_status_word_has_no_brackets(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        # нет активной задачи и стора → терминальный дефолт "stopped" (без скобок)
        assert scr._session_status_word() == "stopped"
        # бейдж оборачивает в скобки
        assert scr._task_status_badge() == "[stopped]"
