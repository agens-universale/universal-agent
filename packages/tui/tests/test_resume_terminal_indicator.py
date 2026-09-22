import pytest


def _store_with_last_indicator(last):
    class _Store:
        def get_task_meta(self, tid):
            return {"tui": {"last_indicator": last}}

        def read_live_messages(self, tid):
            return []

    return _Store()


def _store_with_tui_status(status):
    """Старая сессия: есть meta.tui.status, но НЕТ last_indicator."""

    class _Store:
        def get_task_meta(self, tid):
            return {"tui": {"status": status}}

        def read_live_messages(self, tid):
            return []

    return _Store()


@pytest.mark.asyncio
async def test_resume_old_session_falls_back_to_status(tmp_path):
    """Сессия без last_indicator (создана до фичи) — строка-итог из meta.tui.status;
    elapsed неизвестен → «✓ Выполнил» без «за N»."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "old"
        scr._store = _store_with_tui_status("completed")
        scr.app.active_runner = None
        scr._thinking = None

        scr._restore_terminal_indicator()
        await pilot.pause()
        assert scr._thinking is not None
        assert scr._thinking._render_text().plain == "✓ Выполнил"


@pytest.mark.asyncio
async def test_resume_old_session_stopped_falls_back(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "old"
        scr._store = _store_with_tui_status("stopped")
        scr.app.active_runner = None
        scr._thinking = None

        scr._restore_terminal_indicator()
        await pilot.pause()
        assert scr._thinking is not None
        assert scr._thinking._render_text().plain == "■ Отменено"


@pytest.mark.asyncio
async def test_resume_unrecorded_outcome_no_line(tmp_path):
    """Сессия без записанного исхода (ни last_indicator, ни tui.status, ни
    meta.status) → строку-итог НЕ рисуем (не выдумываем исход)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    class _Store:
        def get_task_meta(self, tid):
            return {"tui": {}}  # исход нигде не записан

        def read_live_messages(self, tid):
            return []

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "unknown"
        scr._store = _Store()
        scr.app.active_runner = None
        scr._thinking = None

        scr._restore_terminal_indicator()
        await pilot.pause()
        assert scr._thinking is None


@pytest.mark.asyncio
async def test_resume_completed_shows_done_with_elapsed(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "t1"
        scr._store = _store_with_last_indicator({"kind": "done", "elapsed": 12})
        scr.app.active_runner = None
        scr._thinking = None

        scr._restore_terminal_indicator()
        await pilot.pause()
        assert scr._thinking is not None
        assert scr._thinking._render_text().plain == "✓ Выполнил за 12с"


@pytest.mark.asyncio
async def test_resume_stopped_shows_cancelled(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "t1"
        scr._store = _store_with_last_indicator({"kind": "cancelled", "elapsed": None})
        scr.app.active_runner = None
        scr._thinking = None

        scr._restore_terminal_indicator()
        await pilot.pause()
        assert scr._thinking._render_text().plain == "■ Отменено"


@pytest.mark.asyncio
async def test_restore_noop_when_running(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "t1"
        scr._store = _store_with_last_indicator({"kind": "done", "elapsed": 5})

        class _R:
            def is_running(self):
                return True

        scr.app.active_runner = _R()
        scr._thinking = None

        scr._restore_terminal_indicator()
        await pilot.pause()
        assert scr._thinking is None, "при живом прогоне не восстанавливаем итог"
