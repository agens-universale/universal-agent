"""Залипший `● идёт`: исход каждого прогона — в его тред на любом выходе.

Мета треда говорит «running», только пока прогон жив. Каждый тест —
путь, на котором исход не доходил до треда прогона (или «running»
попадал в чужой тред).
"""

import json

import pytest
from gc_testkit.tui_support import tui_settings

TID_A = "stal0000000000000000000000000000a"
TID_B = "stal0000000000000000000000000000b"


def _write_task(tmp_path, tid):
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "system_id": "cli"}), encoding="utf-8"
    )
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


class _Runner:
    """Раннер прогона треда `tid`: цель и вид работы — как у AgentRunner."""

    def __init__(self, tid, *, running=True):
        self._running = running
        self._last_target = (tid, "cli")
        self._thread_kind = "run"
        self._stop_requested = False

    def is_running(self):
        return self._running

    def stop(self):
        self._stop_requested = True
        self._running = False


def _app(tmp_path):
    from gc_tui.app import TuiApp

    for tid in (TID_A, TID_B):
        _write_task(tmp_path, tid)
    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )


def _status(scr_or_store, tid):
    store = getattr(scr_or_store, "_store", None) or scr_or_store
    return (store.get_task_meta(tid).get("tui") or {}).get("status")


def _store(tmp_path):
    from gc_tui.services.stores.file_store import FileTaskStore

    return FileTaskStore(threads_dir=str(tmp_path), system="cli")


@pytest.mark.asyncio
async def test_new_from_idle_session_keeps_its_outcome(tmp_path):
    """`/new` из сессии без хода не пишет ей «running»."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(TID_A)
        scr._store.update_tui_meta(TID_A, {"status": "completed"})
        scr._cmd_new()
        await pilot.pause()
        assert _status(_store(tmp_path), TID_A) == "completed"


@pytest.mark.asyncio
async def test_new_during_run_writes_stopped_to_run_thread(tmp_path):
    """`/new` посреди хода: стоп — исход «stopped» в тред прогона."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(TID_A)
        scr._prepare_for_new_run()
        app.active_runner = _Runner(TID_A)
        scr._poll_active()  # тик застал прогон
        scr._stop_and_replace_runner()
        scr._switch_to_empty_state()
        scr._poll_active()
        await pilot.pause()
        assert _status(_store(tmp_path), TID_A) == "stopped"


@pytest.mark.asyncio
async def test_rising_edge_stamps_run_thread_not_open_one(tmp_path):
    """Переход в B до первого тика: «running» — треду прогона A, не B."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(TID_A)
        scr._prepare_for_new_run()
        app.active_runner = _Runner(TID_A)
        scr.attach_active_task(TID_B, is_resume=True)
        scr._poll_active()
        assert _status(scr, TID_B) is None
        assert _status(scr, TID_A) == "running"


@pytest.mark.asyncio
async def test_new_run_before_tick_settles_previous_run(tmp_path):
    """Ход A кончился, тик ещё не видел; Enter в B запускает новый ход
    тем же раннером — исход A всё равно в A."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(TID_A)
        scr._prepare_for_new_run()
        runner = _Runner(TID_A)
        app.active_runner = runner
        scr._poll_active()  # тик застал ход A
        scr.attach_active_task(TID_B, is_resume=True)
        runner._running = False  # ход A кончился между тиками
        scr._prepare_for_new_run()  # Enter в B
        runner._last_target = (TID_B, "cli")
        runner._running = True
        scr._poll_active()
        assert _status(scr, TID_A) == "completed"
        assert _status(scr, TID_B) == "running"


@pytest.mark.asyncio
async def test_instant_failure_after_switch_lands_in_run_thread(tmp_path):
    """Ход A упал сразу (предохранитель), тик его бегущим не застал, а
    открыт уже B: исход «failed» — в A."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(TID_A)
        scr._prepare_for_new_run()
        app.active_runner = _Runner(TID_A, running=False)
        app.last_runner_error = "алиас в предохранителе"
        scr.attach_active_task(TID_B, is_resume=True)
        scr._poll_active()
        assert _status(scr, TID_A) == "failed"


@pytest.mark.asyncio
async def test_quit_mid_run_writes_stopped(tmp_path):
    """Выход из приложения посреди хода — исход «stopped» в тред хода."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(TID_A)
        scr._prepare_for_new_run()
        app.active_runner = _Runner(TID_A)
        scr._poll_active()
    assert _status(_store(tmp_path), TID_A) == "stopped"


# --- чтение: «running» без живого хода — «прервано» -------------------------


def _stamp_running(tmp_path, tid):
    _store(tmp_path).update_tui_meta(tid, {"status": "running"})


async def _sessions_text(pilot, tmp_path):
    from gc_tui.screens.sessions import SessionsScreen

    await pilot.app.push_screen(
        SessionsScreen(threads_dir=str(tmp_path), system="cli")
    )
    await pilot.pause()
    return "\n".join(
        str(s.render())
        for s in pilot.app.screen.query("#sessions-list Static")
    )


@pytest.mark.asyncio
async def test_sessions_stale_running_without_holder_is_interrupted(
    tmp_path,
):
    app = _app(tmp_path)
    _stamp_running(tmp_path, TID_A)
    async with app.run_test() as pilot:
        text = await _sessions_text(pilot, tmp_path)
        assert "■ прервано" in text
        assert "идёт" not in text


@pytest.mark.asyncio
async def test_sessions_running_held_by_live_tui_stays_running(tmp_path):
    from gc_tui.services.session_lock import try_acquire

    app = _app(tmp_path)
    _stamp_running(tmp_path, TID_A)
    assert try_acquire(str(tmp_path), "cli", TID_A, "other-tui")
    async with app.run_test() as pilot:
        text = await _sessions_text(pilot, tmp_path)
        assert "● идёт" in text
        assert "прервано" not in text


@pytest.mark.asyncio
async def test_sessions_own_run_of_other_thread_stays_running(tmp_path):
    """Ход A идёт в этом окне, открыта B: замок A отпущен, но ход жив."""
    app = _app(tmp_path)
    _stamp_running(tmp_path, TID_A)
    async with app.run_test() as pilot:
        pilot.app.screen.attach_active_task(TID_B)
        app.active_runner = _Runner(TID_A)
        text = await _sessions_text(pilot, tmp_path)
        assert "● идёт" in text
        assert "прервано" not in text


def _outcome_text(scr) -> str:
    """Свежая строка итога последнего хода в ленте; индикатора нет."""
    from gc_tui.widgets.turn_line import TurnLine

    assert scr._thinking is None
    (line,) = scr.query(TurnLine).filter(".-latest")
    return line.render().plain


@pytest.mark.asyncio
async def test_reopen_stale_running_shows_interrupted(tmp_path):
    """Переоткрытие после рестарта: строка итога «■ Прервано», статус —
    «interrupted»; мету чтение не переписывает."""
    app = _app(tmp_path)
    _stamp_running(tmp_path, TID_A)
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(TID_A, is_resume=True)
        await pilot.pause()
        assert scr._session_status_word() == "interrupted"
        assert _outcome_text(scr) == "■ Прервано"
        assert _status(scr, TID_A) == "running"


# --- длительность хода: одно значение для ленты и меты ----------------------


@pytest.mark.asyncio
async def test_elapsed_live_equals_reopen_from_turn_record(tmp_path):
    """Длительность хода — одна, из записи итога ядра: ни отсчёт экрана,
    ни свой счёт индикатора её не подменяют. «Выполнил за N» вживую и
    после переоткрытия — одно и то же N; в мете длительности нет."""
    import time

    from gc_core.persistence.turn_outcome import TurnOutcome
    from gc_core.thread_store import (
        append_new_thread_messages,
        append_turn_outcome_record,
    )

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(TID_A, is_resume=True)
        scr._prepare_for_new_run()
        scr._reset_thinking_timer()
        runner = _Runner(TID_A)
        app.active_runner = runner
        scr._poll_active()
        scr._thinking_started_at = time.monotonic() - 42
        scr._thinking._started_at = time.monotonic() - 9
        append_new_thread_messages(
            str(tmp_path),
            "cli",
            TID_A,
            [{"id": "u1", "role": "user", "content": "q"}],
            set(),
        )
        append_turn_outcome_record(
            str(tmp_path),
            "cli",
            TID_A,
            TurnOutcome(
                status="done",
                reason=None,
                opened_by="u1",
                started_at="2026-10-22T11:32:00Z",
                ended_at="",
                duration_sec=5.0,
                error=None,
            ),
            seq=1,
        )
        runner._running = False
        scr._poll_active()
        await pilot.pause()
        live = _outcome_text(scr)
        meta = scr._store.get_task_meta(TID_A)["tui"]["last_indicator"]
        assert live.startswith("✓ Выполнил за 5с")
        assert "elapsed" not in meta
        scr.attach_active_task(TID_B, is_resume=True)
        scr.attach_active_task(TID_A, is_resume=True)
        await pilot.pause()
        assert _outcome_text(scr) == live


@pytest.mark.asyncio
async def test_settle_failure_is_logged_once_not_retried(tmp_path, caplog):
    """Сбой записи исхода не повторяется на каждом тике: флаги закрытия
    снимаются, причина — в лог."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(TID_A)
        scr._prepare_for_new_run()
        app.active_runner = _Runner(TID_A, running=False)
        calls = []

        def _boom(runner, kind):
            calls.append(kind)
            raise RuntimeError("boom")

        scr._on_run_finished = _boom
        scr._poll_active()
        scr._poll_active()
        assert calls == ["run"]
        assert scr._run_pending is False
        assert "boom" in caplog.text


@pytest.mark.asyncio
async def test_sessions_open_thread_unsettled_run_is_not_interrupted(
    tmp_path,
):
    """Ход открытой сессии кончился, тик его ещё не закрыл: в списке —
    не «прервано» (тот же признак живого хода, что у экрана)."""
    from gc_tui.screens.sessions import SessionsScreen

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(TID_A)
        await pilot.app.push_screen(
            SessionsScreen(threads_dir=str(tmp_path), system="cli")
        )
        await pilot.pause()
        sessions = pilot.app.screen
        # без пауз: тик между строками не проходит
        scr._prepare_for_new_run()
        app.active_runner = _Runner(TID_A, running=False)
        (task,) = [
            t for t in sessions._store.list_tasks() if t.thread_id == TID_A
        ]
        assert sessions._task_display_status(task) == "running"
