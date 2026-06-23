"""_cmd_compact: гарды (режим/простой/контент) + запуск compact_now."""
import json
import pytest


def _write_task(tmp_path, tid, content_chars=0):
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "system_id": "cli"}), encoding="utf-8")
    body = "x" * content_chars
    line = json.dumps({"type": "message", "thread_id": tid, "system": "cli", "seq": 1,
                       "ts": "t", "payload": {"role": "user", "content": body}})
    (tmp_path / f"data_cli_{tid}.jsonl").write_text(line + "\n", encoding="utf-8")


class _Runner:
    def __init__(self, running=False):
        self._running = running
        self.compacted = False
    def is_running(self):
        return self._running
    def compact_now(self, tid, system):
        self.compacted = True
        return True


def _chat_cfg():
    return {"model": "M", "backend_api_mode": "chat_completions",
            "summary_params": {"chat_completions_summary_trigger_chars": 1000}}


@pytest.mark.asyncio
async def test_compact_runs_when_idle_and_enough_content(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    tid = "cmp00000000000000000000000000001a"
    _write_task(tmp_path, tid, content_chars=500)   # 50% порога ≥ 20%
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config=_chat_cfg())
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        runner = _Runner(running=False)
        pilot.app.active_runner = runner
        scr._cmd_compact()
        assert runner.compacted is True


@pytest.mark.asyncio
async def test_compact_blocked_when_running(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    tid = "cmp00000000000000000000000000002a"
    _write_task(tmp_path, tid, content_chars=500)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config=_chat_cfg())
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        runner = _Runner(running=True)
        pilot.app.active_runner = runner
        scr._cmd_compact()
        assert runner.compacted is False


@pytest.mark.asyncio
async def test_compact_blocked_low_content(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    tid = "cmp00000000000000000000000000003a"
    _write_task(tmp_path, tid, content_chars=50)    # 5% порога < 20%
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config=_chat_cfg())
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        runner = _Runner(running=False)
        pilot.app.active_runner = runner
        scr._cmd_compact()
        assert runner.compacted is False


@pytest.mark.asyncio
async def test_compact_blocked_in_responses(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    tid = "cmp00000000000000000000000000004a"
    _write_task(tmp_path, tid, content_chars=500)
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "responses"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        runner = _Runner(running=False)
        pilot.app.active_runner = runner
        scr._cmd_compact()
        assert runner.compacted is False
