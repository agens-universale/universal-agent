"""Стамп терминального статуса в meta.tui.status на спадающем фронте прогона.

Источник «stopped» — app-флаг `last_stop_requested` (НЕ `runner.stop_requested`):
при /stop runner заменяется (`active_runner=None`), поэтому опираться на сам
объект раннера в момент стампа нельзя (баг: давало «completed» вместо «stopped»).
"""

from gc_testkit.tui_support import tui_settings
import json

import pytest


def _write_task(tmp_path, tid):
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "system_id": "cli"}), encoding="utf-8"
    )
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


class _Runner:
    def __init__(self, running):
        self._running = running

    def is_running(self):
        return self._running

    def stop(self):
        self._running = False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runner,last_stop,last_error,expected",
    [
        (_Runner(running=False), False, None, "completed"),
        (None, True, None, "stopped"),  # /stop заменил runner на None
        (_Runner(running=False), False, "boom", "failed"),
    ],
)
async def test_falling_edge_stamps_status(
    tmp_path, runner, last_stop, last_error, expected
):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "stmp0000000000000000000000000001a"
    _write_task(tmp_path, tid)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        scr._was_running = True  # был running на прошлом тике
        pilot.app.active_runner = runner
        pilot.app.last_stop_requested = last_stop
        pilot.app.last_runner_error = last_error
        scr._poll_active()
        await pilot.pause()
        meta = scr._store.get_task_meta(tid)
        assert (meta.get("tui") or {}).get("status") == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,expected", [(None, "completed"), ("boom", "failed")]
)
async def test_finished_before_first_poll_stamps_status(
    tmp_path, error, expected
):
    from gc_tui.app import TuiApp

    tid = "fast00000000000000000000000000001"
    _write_task(tmp_path, tid)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        scr._prepare_for_new_run()
        scr._was_running = False
        runner = _Runner(running=False)
        runner._last_target = (tid, "cli")
        app.active_runner = runner
        app.last_runner_error = error
        scr._poll_active()
        assert scr._store.get_task_meta(tid)["tui"]["status"] == expected


@pytest.mark.asyncio
async def test_compact_transition_does_not_stamp(tmp_path):
    """Завершение /compact (runner._thread_kind=='compact') НЕ перетирает
    терминальный статус: falling-edge должен пропустить compact-переход."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "stmp0000000000000000000000000003a"
    _write_task(tmp_path, tid)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        scr._store.update_tui_meta(
            tid, {"status": "stopped"}
        )  # сессия была остановлена
        scr._was_running = True
        r = _Runner(running=False)
        r._thread_kind = "compact"
        pilot.app.active_runner = r
        pilot.app.last_stop_requested = False
        pilot.app.last_runner_error = None
        scr._poll_active()
        await pilot.pause()
        meta = scr._store.get_task_meta(tid)
        assert (meta.get("tui") or {}).get(
            "status"
        ) == "stopped"  # НЕ completed


@pytest.mark.asyncio
async def test_compact_with_stuck_running_meta_is_not_finalized(tmp_path):
    """/compact при залипшем meta.status=running не закрывает ход на
    каждом холостом тике (как и в develop: только по _was_running)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "stmp0000000000000000000000000005a"
    _write_task(tmp_path, tid)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        scr._store.update_tui_meta(tid, {"status": "running"})
        scr._was_running = False
        r = _Runner(running=False)
        r._thread_kind = "compact"
        r._last_target = (tid, "cli")
        pilot.app.active_runner = r
        scr._skills_cache = "cached"
        for _ in range(3):
            scr._poll_active()
        assert scr._skills_cache == "cached"


@pytest.mark.asyncio
async def test_prepare_for_new_run_stamps_running_over_completed(tmp_path):
    """B1: новый ход сбрасывает meta.tui.status=completed → running и чистит
    last_indicator, иначе список сессий/бейдж врут после прошлого финиша."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "stmp0000000000000000000000000004a"
    _write_task(tmp_path, tid)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        scr._store.update_tui_meta(
            tid,
            {
                "status": "completed",
                "last_indicator": {"kind": "done", "elapsed": 32},
            },
        )
        scr._prepare_for_new_run()
        meta = scr._store.get_task_meta(tid)
        tui = meta.get("tui") or {}
        assert tui.get("status") == "running"
        assert tui.get("last_indicator") is None


@pytest.mark.asyncio
async def test_rising_edge_stamps_running(tmp_path):
    """Восходящий фронт poll: was_running False→True → status=running."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "stmp0000000000000000000000000005a"
    _write_task(tmp_path, tid)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        scr._store.update_tui_meta(
            tid,
            {
                "status": "completed",
                "last_indicator": {"kind": "done", "elapsed": 7},
            },
        )
        scr._was_running = False
        pilot.app.active_runner = _Runner(running=True)
        scr._poll_active()
        await pilot.pause()
        meta = scr._store.get_task_meta(tid)
        tui = meta.get("tui") or {}
        assert tui.get("status") == "running"
        assert tui.get("last_indicator") is None


@pytest.mark.asyncio
async def test_session_status_word_reads_tui_running(tmp_path):
    """meta.tui.status=running без живого runner (crash mid-run) → 'running'."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "stmp0000000000000000000000000006a"
    _write_task(tmp_path, tid)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        scr._store.update_tui_meta(tid, {"status": "running"})
        pilot.app.active_runner = None
        assert scr._session_status_word() == "running"
