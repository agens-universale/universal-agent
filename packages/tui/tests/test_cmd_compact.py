"""_cmd_compact: гарды (режим/простой/контент) + запуск compact_now.

Антиспам по контенту = count-vs-baseline: разрешаем, только если число
сообщений выросло с момента последнего сжатия (meta.tui.last_compact_msg_count).
"""

import json

import pytest


def _write_task(tmp_path, tid, n_messages=1, last_compact=None):
    # Антиспам /compact: runtime.live_msg_count vs tui.last_compact_msg_count.
    runtime = {"live_msg_count": n_messages}
    tui = {}
    if last_compact is not None:
        tui["last_compact_msg_count"] = last_compact
    meta = {"thread_id": tid, "system_id": "cli", "runtime": runtime}
    if tui:
        meta["tui"] = tui
    (tmp_path / f"meta_cli_{tid}.json").write_text(json.dumps(meta), encoding="utf-8")
    lines = []
    for i in range(1, n_messages + 1):
        lines.append(
            json.dumps(
                {
                    "type": "message",
                    "thread_id": tid,
                    "system": "cli",
                    "seq": i,
                    "ts": "t",
                    "payload": {"role": "user", "content": f"m{i}"},
                }
            )
        )
    (tmp_path / f"data_cli_{tid}.jsonl").write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")


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
    return {"model": "M", "backend_api_mode": "chat_completions"}


@pytest.mark.asyncio
async def test_compact_runs_when_grown_since_last_compact(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "cmp00000000000000000000000000001a"
    _write_task(tmp_path, tid, n_messages=3, last_compact=1)  # 3 > 1 → выросло
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config=_chat_cfg())
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        runner = _Runner(running=False)
        pilot.app.active_runner = runner
        scr._cmd_compact()
        assert runner.compacted is True


@pytest.mark.asyncio
async def test_compact_blocked_when_nothing_new_since_compact(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "cmp00000000000000000000000000002a"
    _write_task(tmp_path, tid, n_messages=3, last_compact=3)  # 3 <= 3 → нет нового
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config=_chat_cfg())
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        runner = _Runner(running=False)
        pilot.app.active_runner = runner
        scr._cmd_compact()
        assert runner.compacted is False


@pytest.mark.asyncio
async def test_compact_runs_when_never_compacted(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "cmp00000000000000000000000000005a"
    _write_task(tmp_path, tid, n_messages=2, last_compact=None)  # baseline нет → можно
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config=_chat_cfg())
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        runner = _Runner(running=False)
        pilot.app.active_runner = runner
        scr._cmd_compact()
        assert runner.compacted is True


@pytest.mark.asyncio
async def test_compact_resets_thinking_timer(tmp_path):
    """Якорь «сжимаю…» сбрасывается на старте /compact, иначе секунды идут от
    прошлого хода (баг: показывало (40с) вместо (0с))."""
    pytest.importorskip("textual")
    import time

    from gc_tui.app import TuiApp

    tid = "cmp00000000000000000000000000006a"
    _write_task(tmp_path, tid, n_messages=3, last_compact=1)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config=_chat_cfg())
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        runner = _Runner(running=False)
        pilot.app.active_runner = runner
        scr._thinking_started_at = time.monotonic() - 100  # устаревший якорь
        scr._cmd_compact()
        assert runner.compacted is True
        assert scr._thinking_started_at > time.monotonic() - 5  # сброшен на ~сейчас


@pytest.mark.asyncio
async def test_compact_blocked_when_running(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "cmp00000000000000000000000000003a"
    _write_task(tmp_path, tid, n_messages=3)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config=_chat_cfg())
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        runner = _Runner(running=True)
        pilot.app.active_runner = runner
        scr._cmd_compact()
        assert runner.compacted is False


@pytest.mark.asyncio
async def test_compact_blocked_in_responses(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "cmp00000000000000000000000000004a"
    _write_task(tmp_path, tid, n_messages=3)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M", "backend_api_mode": "responses"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        runner = _Runner(running=False)
        pilot.app.active_runner = runner
        scr._cmd_compact()
        assert runner.compacted is False
