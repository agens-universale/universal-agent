"""TUI-проводка session lock: deny resume, badge, release на /new, empty-state."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from thread_store import append_new_thread_messages
from tui.services.session_lock import (
    BUSY_MESSAGE,
    lock_path,
    try_acquire,
)


def _seed(threads_dir, tid, prompt="hello"):
    append_new_thread_messages(
        str(threads_dir), "cli", tid,
        [{"role": "user", "content": prompt, "id": f"m_{tid}"}], set(),
    )
    meta = {
        "thread_id": tid,
        "system": "cli",
        "status": "stopped",
        "updated_at": "2026-05-30T10:00:00+00:00",
        "messages_count": 1,
    }
    (Path(threads_dir) / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8",
    )


def _rows_text(pilot):
    return "\n".join(
        str(s.render()) for s in pilot.app.screen.query("#sessions-list Static")
    )


@pytest.mark.asyncio
async def test_attach_denies_when_held_by_other(tmp_path):
    pytest.importorskip("textual")
    tid = "aaaaaaaa"
    _seed(tmp_path, tid)
    assert try_acquire(str(tmp_path), "cli", tid, "foreign") is True

    from tui.app import TuiApp

    notifications = []
    async with TuiApp(threads_dir=str(tmp_path), system="cli").run_test() as pilot:
        pilot.app.notify = lambda msg, **kw: notifications.append(msg)
        scr = pilot.app.screen
        ok = scr.attach_active_task(tid, is_resume=True)
        assert ok is False
        assert scr._active_task_id is None
        assert pilot.app.tui_state.active_task_id is None
        assert BUSY_MESSAGE in notifications


@pytest.mark.asyncio
async def test_attach_allows_own_tui_id(tmp_path):
    pytest.importorskip("textual")
    tid = "bbbbbbbb"
    _seed(tmp_path, tid)

    from tui.app import TuiApp

    async with TuiApp(threads_dir=str(tmp_path), system="cli").run_test() as pilot:
        assert try_acquire(str(tmp_path), "cli", tid, pilot.app.tui_id) is True
        scr = pilot.app.screen
        assert scr.attach_active_task(tid, is_resume=True) is True
        assert scr._active_task_id == tid


@pytest.mark.asyncio
async def test_new_releases_lock(tmp_path):
    pytest.importorskip("textual")
    tid = "cccccccc"
    _seed(tmp_path, tid)

    from tui.app import TuiApp

    async with TuiApp(threads_dir=str(tmp_path), system="cli").run_test() as pilot:
        scr = pilot.app.screen
        assert scr.attach_active_task(tid, is_resume=True) is True
        assert Path(lock_path(str(tmp_path), "cli", tid)).exists()
        scr._switch_to_empty_state()
        assert not Path(lock_path(str(tmp_path), "cli", tid)).exists()
        assert scr._active_task_id is None


@pytest.mark.asyncio
async def test_empty_state_does_not_create_lock(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp

    async with TuiApp(threads_dir=str(tmp_path), system="cli").run_test() as pilot:
        await pilot.pause()
        locks = list(Path(tmp_path).glob("lock_*.json"))
        assert locks == []
        assert pilot.app.screen._active_task_id is None


@pytest.mark.asyncio
async def test_sessions_shows_busy_badge(tmp_path):
    pytest.importorskip("textual")
    tid = "dddddddd"
    _seed(tmp_path, tid, "занятая сессия")
    assert try_acquire(str(tmp_path), "cli", tid, "foreign") is True

    from tui.app import TuiApp
    from tui.screens.sessions import SessionsScreen

    async with TuiApp(threads_dir=str(tmp_path), system="cli").run_test() as pilot:
        await pilot.app.push_screen(
            SessionsScreen(threads_dir=str(tmp_path), system="cli")
        )
        await pilot.pause()
        text = _rows_text(pilot)
        assert "🔒 занято" in text


@pytest.mark.asyncio
async def test_sessions_resume_denies_busy(tmp_path):
    pytest.importorskip("textual")
    tid_a = "eeeeeeee"
    tid_b = "ffffffff"
    _seed(tmp_path, tid_a, "моя")
    _seed(tmp_path, tid_b, "чужая")
    assert try_acquire(str(tmp_path), "cli", tid_b, "foreign") is True

    from tui.app import TuiApp
    from tui.screens.sessions import SessionsScreen

    notifications = []
    async with TuiApp(threads_dir=str(tmp_path), system="cli").run_test() as pilot:
        pilot.app.notify = lambda msg, **kw: notifications.append(msg)
        scr = pilot.app.screen
        assert scr.attach_active_task(tid_a, is_resume=True) is True
        await pilot.app.push_screen(
            SessionsScreen(threads_dir=str(tmp_path), system="cli")
        )
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, SessionsScreen)
        # выбрать B (порядок по updated_at одинаковый — по id в list)
        tasks = screen._visible_tasks()
        idx = next(i for i, t in enumerate(tasks) if t.thread_id == tid_b)
        screen._selected_idx = idx % 25
        screen._resume_thread(tid_b)
        assert BUSY_MESSAGE in notifications
        # A остаётся активной после pop (resume deny не должен её сбрасывать)
        # deny не pop'ает? SPEC: не открывать. Можно оставить на sessions или pop.
        # Канон: не attach B; A активна. Реализация: notify и остаёмся / возвращаемся.
        assert pilot.app.tui_state.active_task_id == tid_a
        assert Path(lock_path(str(tmp_path), "cli", tid_a)).exists()


@pytest.mark.asyncio
async def test_sessions_switch_releases_previous(tmp_path):
    pytest.importorskip("textual")
    tid_a = "11111111"
    tid_b = "22222222"
    _seed(tmp_path, tid_a, "A")
    _seed(tmp_path, tid_b, "B")

    from tui.app import TuiApp
    from tui.screens.sessions import SessionsScreen

    async with TuiApp(threads_dir=str(tmp_path), system="cli").run_test() as pilot:
        scr = pilot.app.screen
        assert scr.attach_active_task(tid_a, is_resume=True) is True
        assert Path(lock_path(str(tmp_path), "cli", tid_a)).exists()
        await pilot.app.push_screen(
            SessionsScreen(threads_dir=str(tmp_path), system="cli")
        )
        await pilot.pause()
        screen = pilot.app.screen
        screen._resume_thread(tid_b)
        assert pilot.app.tui_state.active_task_id == tid_b
        assert not Path(lock_path(str(tmp_path), "cli", tid_a)).exists()
        assert Path(lock_path(str(tmp_path), "cli", tid_b)).exists()
