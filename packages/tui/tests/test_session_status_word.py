from gc_testkit.develop_files import record_from_meta
import pytest


@pytest.mark.asyncio
async def test_status_word_has_no_brackets(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp(
        threads_dir=str(tmp_path / "threads")
    ).run_test() as pilot:
        scr = pilot.app.screen
        # нет активной задачи и стора → исход неизвестен → None (не выдумываем "stopped")
        assert scr._session_status_word() is None
        # бейдж тоже None (нечего показывать)
        assert scr._task_status_badge() is None


@pytest.mark.asyncio
async def test_status_word_reads_real_terminal_status(tmp_path):
    """Записанный исход возвращается как слово (со скобками в бейдже)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    class _Store:
        def get_record(self, tid):
            return record_from_meta({"tui": {"status": "completed"}})

    async with TuiApp(
        threads_dir=str(tmp_path / "threads")
    ).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "t1"
        scr._store = _Store()
        assert scr._session_status_word() == "completed"
        assert scr._task_status_badge() == "[completed]"
