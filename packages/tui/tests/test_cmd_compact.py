"""_cmd_compact: гарды (режим/простой/контент) + запуск compact_now.

Антиспам по контенту = count-vs-baseline: разрешаем, только если число
сообщений выросло с момента последнего сжатия (meta.tui.last_compact_msg_count).
"""

import asyncio
import json

import pytest
from gc_core.summarize.summary_validation import SummaryRejectedError
from gc_testkit.tui_support import tui_settings


def _write_task(tmp_path, tid, n_messages=1, last_compact=None):
    # Антиспам /compact: runtime.live_msg_count vs tui.last_compact_msg_count.
    runtime = {"live_msg_count": n_messages}
    tui = {}
    if last_compact is not None:
        tui["last_compact_msg_count"] = last_compact
    meta = {"thread_id": tid, "system_id": "cli", "runtime": runtime}
    if tui:
        meta["tui"] = tui
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
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
    (tmp_path / f"data_cli_{tid}.jsonl").write_text(
        ("\n".join(lines) + "\n") if lines else "", encoding="utf-8"
    )


class _Runner:
    def __init__(self, running=False):
        self._running = running
        self.compacted = False

    def is_running(self):
        return self._running

    def compact_now(self, tid, system):
        self.compacted = True
        return True


def _bar_up(scr) -> bool:
    """Бар индикатора поднят (S10 §3.3): работа видна только в нём."""
    from textual.containers import Vertical

    return scr.query_one("#indicator-bar", Vertical).has_class("-turn")


def _chat_cfg():
    return {"agent.model": "M", "backend.api_mode": "chat_completions"}


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (
            SummaryRejectedError("summary repair produced no usable summary"),
            "Сжатие не удалось. История не изменена.",
        ),
        (
            RuntimeError("archive write failed"),
            "RuntimeError: archive write failed",
        ),
    ],
    ids=["rejected-summary", "archive-failure"],
)
async def test_compact_displays_the_appropriate_error(
    tmp_path, error, message
):
    from gc_tui.app import TuiApp
    from gc_tui.widgets.trace_row import TraceRow

    class FailingExecutor:
        async def acompact_thread(self, thread_id, *, system):
            raise error

        def stop_run(self):
            pass

    tid = "cmp00000000000000000000000001a"
    _write_task(tmp_path, tid, n_messages=3)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(_chat_cfg()),
    )
    app._executor_factory = FailingExecutor

    async with app.run_test() as pilot:
        screen = pilot.app.screen
        screen.attach_active_task(tid)
        screen._cmd_compact()
        await asyncio.to_thread(app.active_runner.wait, 2.0)
        assert not app.active_runner.is_running()
        screen._poll_active()
        await pilot.pause()
        # /compact — не ход: строки итога нет, причина — строкой ошибки.
        rows = [
            row._content
            for row in screen.query(TraceRow)
            if row.has_class("line-error")
        ]
        assert rows == [f"runtime: {message}"]


@pytest.mark.asyncio
async def test_compact_runs_when_grown_since_last_compact(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "cmp00000000000000000000000000001a"
    _write_task(tmp_path, tid, n_messages=3, last_compact=1)  # 3 > 1 → выросло
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(_chat_cfg()),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        runner = _Runner(running=False)
        pilot.app.active_runner = runner
        assert not _bar_up(scr)
        scr._cmd_compact()
        assert runner.compacted is True
        # Принятый /compact — работа: бар поднят тем же вызовом.
        assert scr._run_pending is True
        assert _bar_up(scr)


@pytest.mark.asyncio
async def test_compact_blocked_when_nothing_new_since_compact(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "cmp00000000000000000000000000002a"
    _write_task(
        tmp_path, tid, n_messages=3, last_compact=3
    )  # 3 <= 3 → нет нового
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(_chat_cfg()),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        runner = _Runner(running=False)
        pilot.app.active_runner = runner
        scr._cmd_compact()
        assert runner.compacted is False
        assert scr._run_pending is False
        assert not _bar_up(scr)


@pytest.mark.asyncio
async def test_compact_runs_when_never_compacted(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "cmp00000000000000000000000000005a"
    _write_task(
        tmp_path, tid, n_messages=2, last_compact=None
    )  # baseline нет → можно
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(_chat_cfg()),
    )
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
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(_chat_cfg()),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        runner = _Runner(running=False)
        pilot.app.active_runner = runner
        scr._thinking_started_at = time.monotonic() - 100  # устаревший якорь
        scr._cmd_compact()
        assert runner.compacted is True
        assert (
            scr._thinking_started_at > time.monotonic() - 5
        )  # сброшен на ~сейчас


@pytest.mark.asyncio
async def test_compact_blocked_when_running(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "cmp00000000000000000000000000003a"
    _write_task(tmp_path, tid, n_messages=3)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(_chat_cfg()),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        runner = _Runner(running=True)
        pilot.app.active_runner = runner
        scr._cmd_compact()
        assert runner.compacted is False
        # Бар — у идущего прогона, не у отказанного /compact.
        assert scr._run_pending is False


@pytest.mark.asyncio
async def test_compact_blocked_in_responses(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "cmp00000000000000000000000000004a"
    _write_task(tmp_path, tid, n_messages=3)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "responses"}
        ),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        runner = _Runner(running=False)
        pilot.app.active_runner = runner
        scr._cmd_compact()
        assert runner.compacted is False
        assert scr._run_pending is False
        assert not _bar_up(scr)


@pytest.mark.asyncio
async def test_compact_without_session_raises_no_bar(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(_chat_cfg()),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        runner = _Runner(running=False)
        pilot.app.active_runner = runner
        scr._cmd_compact()
        assert runner.compacted is False
        assert scr._run_pending is False
        assert not _bar_up(scr)


class _BusyCompact(_Runner):
    """Воркер раннера ещё занят: `compact_now` отказывает."""

    def compact_now(self, tid, system):
        return False


@pytest.mark.asyncio
async def test_compact_refused_by_runner_raises_no_bar(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "cmp00000000000000000000000000007a"
    _write_task(tmp_path, tid, n_messages=3)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(_chat_cfg()),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        pilot.app.active_runner = _BusyCompact(running=False)
        scr._cmd_compact()
        assert scr._run_pending is False
        assert not _bar_up(scr)


@pytest.mark.asyncio
async def test_compact_after_settled_run_gets_bar(tmp_path):
    """`/compact` сразу после зафиксированного прогона того же раннера:
    тик видит сжатие идущей работой — бар стоит и после первого тика."""
    import threading

    from gc_tui.app import TuiApp
    from gc_tui.services.agent_runner import AgentRunner

    release = threading.Event()

    class _Ex:
        async def acompact_thread(self, tid, system=None):
            release.wait(5)
            return True

    tid = "cmp00000000000000000000000000009a"
    _write_task(tmp_path, tid, n_messages=3, last_compact=1)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(_chat_cfg()),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        runner = AgentRunner(executor=_Ex())
        runner._last_target = (tid, "cli")
        pilot.app.active_runner = runner
        scr._settled_run = (runner, runner.run_seq)  # прошлый прогон закрыт
        try:
            scr._cmd_compact()
            scr._poll_tick()
            assert scr._watched_runner is runner
            assert scr._bar_wanted()
            assert _bar_up(scr)
        finally:
            release.set()
            runner._thread.join()
