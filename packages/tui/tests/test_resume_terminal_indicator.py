import pytest


def _store_with_last_indicator(last):
    class _Store:
        def get_task_meta(self, tid):
            return {"tui": {"last_indicator": last}}

    return _Store()


def _store_with_tui_status(status):
    """Старая сессия: есть meta.tui.status, но НЕТ last_indicator."""

    class _Store:
        def get_task_meta(self, tid):
            return {"tui": {"status": status}}

    return _Store()


@pytest.mark.asyncio
async def test_resume_old_session_falls_back_to_status(tmp_path):
    """Сессия без last_indicator (создана до фичи) — строка-итог из meta.tui.status;
    elapsed неизвестен → «✓ Выполнил» без «за N»."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp(
        threads_dir=str(tmp_path / "threads")
    ).run_test() as pilot:
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

    async with TuiApp(
        threads_dir=str(tmp_path / "threads")
    ).run_test() as pilot:
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

    async with TuiApp(
        threads_dir=str(tmp_path / "threads")
    ).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "unknown"
        scr._store = _Store()
        scr.app.active_runner = None
        scr._thinking = None

        scr._restore_terminal_indicator()
        await pilot.pause()
        assert scr._thinking is None


def _outcome(status, reason=None):
    from gc_core.persistence.turn_outcome import TurnOutcome

    return TurnOutcome(
        status=status,
        reason=reason,
        opened_by=None,
        started_at="2026-10-22T11:32:00Z",
        ended_at="",
        duration_sec=12.0,
        error=None,
    )


@pytest.mark.asyncio
async def test_resume_completed_shows_done_with_elapsed(tmp_path):
    """Запись итога последнего хода — строка из неё: длительность из
    записи; старые поля меты (`kind`, `elapsed`) не читаются."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp(
        threads_dir=str(tmp_path / "threads")
    ).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "t1"
        scr._store = _store_with_last_indicator(
            {"kind": "stopped_steps", "elapsed": 99}
        )
        scr._last_turn_outcome = _outcome("done")
        scr.app.active_runner = None
        scr._thinking = None

        scr._restore_terminal_indicator()
        await pilot.pause()
        assert scr._thinking is not None
        assert scr._thinking._render_text().plain == "✓ Выполнил за 12с"


@pytest.mark.asyncio
async def test_resume_stopped_shows_cancelled(tmp_path):
    pytest.importorskip("textual")
    from gc_core.persistence.turn_outcome import USER_STOP
    from gc_tui.app import TuiApp

    async with TuiApp(
        threads_dir=str(tmp_path / "threads")
    ).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "t1"
        scr._store = _store_with_last_indicator(None)
        scr._last_turn_outcome = _outcome("stopped", USER_STOP)
        scr.app.active_runner = None
        scr._thinking = None

        scr._restore_terminal_indicator()
        await pilot.pause()
        assert scr._thinking._render_text().plain == "■ Отменено"


@pytest.mark.asyncio
async def test_resume_old_last_indicator_without_status_draws_nothing(
    tmp_path,
):
    """Старая мета: `last_indicator.kind` без записи и без статуса —
    исход не записан, строки нет (поле больше не источник)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp(
        threads_dir=str(tmp_path / "threads")
    ).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "t1"
        scr._store = _store_with_last_indicator(
            {"kind": "done", "elapsed": 12}
        )
        scr.app.active_runner = None
        scr._thinking = None

        scr._restore_terminal_indicator()
        await pilot.pause()
        assert scr._thinking is None


@pytest.mark.asyncio
async def test_restore_noop_when_running(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp(
        threads_dir=str(tmp_path / "threads")
    ).run_test() as pilot:
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
        assert scr._thinking is None, (
            "при живом прогоне не восстанавливаем итог"
        )
