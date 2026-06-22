"""Стамп терминального статуса в meta.tui.status на спадающем фронте прогона."""
import json
import pytest


def _write_task(tmp_path, tid):
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "system_id": "cli"}), encoding="utf-8")
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


class _Runner:
    def __init__(self, running, stop_requested=False):
        self._running = running
        self.stop_requested = stop_requested
    def is_running(self):
        return self._running


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_requested,last_error,expected", [
    (False, None, "completed"),
    (True, None, "stopped"),
    (False, "boom", "failed"),
])
async def test_falling_edge_stamps_status(tmp_path, stop_requested, last_error, expected):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    tid = "stmp0000000000000000000000000001a"
    _write_task(tmp_path, tid)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        # был running на прошлом тике
        scr._was_running = True
        pilot.app.active_runner = _Runner(running=False, stop_requested=stop_requested)
        pilot.app.last_runner_error = last_error
        scr._poll_active()
        await pilot.pause()
        meta = scr._store.get_task_meta(tid)
        assert (meta.get("tui") or {}).get("status") == expected
