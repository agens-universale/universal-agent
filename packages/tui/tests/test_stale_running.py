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
        self.run_started_at = None

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
