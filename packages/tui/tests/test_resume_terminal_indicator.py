"""Переоткрытие сессии: строка итога прошлого хода — свежей строкой в
ленте (S10 §4.7), индикатор не монтируется."""

import pytest
from gc_testkit.develop_files import record_from_meta


def _lines(scr) -> list[str]:
    from gc_tui.widgets.turn_line import TurnLine

    return [line.render().plain for line in scr.query(TurnLine)]


def _fresh(scr):
    from gc_tui.widgets.turn_line import TurnLine

    (line,) = scr.query(TurnLine).filter(".-latest")
    return line


def _store_with_last_indicator(last):
    class _Store:
        def get_record(self, tid):
            return record_from_meta({"tui": {"last_indicator": last}})

    return _Store()


def _store_with_tui_status(status):
    """Старая сессия: есть meta.tui.status, но НЕТ last_indicator."""

    class _Store:
        def get_record(self, tid):
            return record_from_meta({"tui": {"status": status}})

    return _Store()


@pytest.mark.asyncio
async def test_resume_old_session_falls_back_to_status(tmp_path):
    """Сессия без last_indicator (создана до фичи) — строка итога из
    meta.tui.status в конце ленты; elapsed неизвестен → «✓ Выполнил» без
    «за N»."""
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

        scr._restore_latest_turn_line()
        await pilot.pause()
        assert scr._thinking is None
        assert _lines(scr) == ["✓ Выполнил"]
        assert _fresh(scr).recorded is False


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

        scr._restore_latest_turn_line()
        await pilot.pause()
        assert scr._thinking is None
        assert _lines(scr) == ["■ Отменено"]


@pytest.mark.asyncio
async def test_resume_unrecorded_outcome_no_line(tmp_path):
    """Сессия без записанного исхода (ни last_indicator, ни tui.status, ни
    meta.status) → строку-итог НЕ рисуем (не выдумываем исход)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    class _Store:
        def get_record(self, tid):
            return record_from_meta({"tui": {}})  # исход нигде не записан

    async with TuiApp(
        threads_dir=str(tmp_path / "threads")
    ).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "unknown"
        scr._store = _Store()
        scr.app.active_runner = None
        scr._thinking = None

        scr._restore_latest_turn_line()
        await pilot.pause()
        assert scr._thinking is None
        assert _lines(scr) == []


def _mount_record(scr, outcome):
    """Запись итога дочитана рендером: строка на её месте в ленте."""
    scr._last_turn_outcome = outcome
    scr._mount_turn_line(outcome)


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
        _mount_record(scr, _outcome("done"))
        scr.app.active_runner = None
        scr._thinking = None

        scr._restore_latest_turn_line()
        await pilot.pause()
        assert scr._thinking is None
        assert _lines(scr) == ["✓ Выполнил за 12с"]


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
        _mount_record(scr, _outcome("stopped", USER_STOP))
        scr.app.active_runner = None
        scr._thinking = None

        scr._restore_latest_turn_line()
        await pilot.pause()
        assert scr._thinking is None
        assert _lines(scr) == ["■ Отменено"]


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

        scr._restore_latest_turn_line()
        await pilot.pause()
        assert scr._thinking is None
        assert _lines(scr) == []


@pytest.mark.asyncio
async def test_resume_fresh_line_shows_summary_from_meta(tmp_path):
    """Сводка правок последнего хода — из `last_indicator.edits` того же
    хода, справа у свежей строки."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    async with TuiApp(
        threads_dir=str(tmp_path / "threads")
    ).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "t1"
        scr._store = _store_with_last_indicator({"edits": [2, 5, 6]})
        _mount_record(scr, _outcome("done"))
        scr.app.active_runner = None
        scr._thinking = None

        scr._restore_latest_turn_line()
        await pilot.pause()
        line = _fresh(scr)
        assert line.summary == (2, 5, 6)
        assert line.render().plain.endswith("изменено 2 файла · +5 −6")
        assert list(scr.query(ThinkingIndicator)) == []


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

        scr._restore_latest_turn_line()
        await pilot.pause()
        assert scr._thinking is None, (
            "при живом прогоне не восстанавливаем итог"
        )
        assert _lines(scr) == []
