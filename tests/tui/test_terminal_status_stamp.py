"""Стамп терминального статуса в meta.tui.status на спадающем фронте прогона.

Источник «stopped» — app-флаг `last_stop_requested` (НЕ `runner.stop_requested`):
при /stop runner заменяется (`active_runner=None`), поэтому опираться на сам
объект раннера в момент стампа нельзя (баг: давало «completed» вместо «stopped»).
"""
import json
import pytest


def _write_task(tmp_path, tid):
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "system_id": "cli"}), encoding="utf-8")
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


class _Runner:
    def __init__(self, running):
        self._running = running

    def is_running(self):
        return self._running

    def stop(self):
        self._running = False


@pytest.mark.asyncio
@pytest.mark.parametrize("runner,last_stop,last_error,expected", [
    (_Runner(running=False), False, None, "completed"),
    (None, True, None, "stopped"),     # /stop заменил runner на None
    (_Runner(running=False), False, "boom", "failed"),
])
async def test_falling_edge_stamps_status(tmp_path, runner, last_stop, last_error, expected):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    tid = "stmp0000000000000000000000000001a"
    _write_task(tmp_path, tid)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        scr._was_running = True               # был running на прошлом тике
        pilot.app.active_runner = runner
        pilot.app.last_stop_requested = last_stop
        pilot.app.last_runner_error = last_error
        scr._poll_active()
        await pilot.pause()
        meta = scr._store.get_task_meta(tid)
        assert (meta.get("tui") or {}).get("status") == expected


@pytest.mark.asyncio
async def test_stop_and_replace_sets_stop_flag(tmp_path):
    """`_stop_and_replace_runner` живого прогона взводит app.last_stop_requested →
    следующий спадающий фронт стампит «stopped» (а не «completed»)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    tid = "stmp0000000000000000000000000002a"
    _write_task(tmp_path, tid)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        pilot.app.active_runner = _Runner(running=True)
        pilot.app.last_stop_requested = False
        scr._was_running = True
        scr._stop_and_replace_runner()        # юзер нажал /stop по живому прогону
        assert pilot.app.last_stop_requested is True
        scr._poll_active()                    # active_runner=None → спадающий фронт
        await pilot.pause()
        meta = scr._store.get_task_meta(tid)
        assert (meta.get("tui") or {}).get("status") == "stopped"
