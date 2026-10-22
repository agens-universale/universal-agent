"""Индикатор живёт в фиксированной #indicator-bar (вне скролла #trace) —
растущий стрим не может на него наложиться."""

from gc_testkit.tui_support import tui_settings
import json
from pathlib import Path

import pytest


def _write_task(tmp_path, tid):
    meta = {
        "thread_id": tid,
        "status": "running",
        "updated_at": "2026-06-15T12:00:00+00:00",
        "messages_count": 0,
    }
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    (Path(tmp_path) / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


@pytest.mark.asyncio
async def test_indicator_docked_not_in_trace(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator
    from textual.containers import Vertical, VerticalScroll

    tid = "dock1"
    _write_task(tmp_path, tid)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "chat_completions"}
        ),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        # Индикатор — только у идущей работы: тик покоя его снял бы.
        pilot.app.active_runner = _Run(tid)
        scr.attach_active_task(tid)
        scr._show_thinking_indicator(state="thinking")
        await pilot.pause()
        bar = pilot.app.query_one("#indicator-bar", Vertical)
        trace = pilot.app.query_one("#trace", VerticalScroll)
        assert any(isinstance(w, ThinkingIndicator) for w in bar.children)
        assert not any(
            isinstance(w, ThinkingIndicator) for w in trace.children
        )


@pytest.mark.asyncio
async def test_trace_row_lands_in_trace_with_indicator_docked(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator
    from gc_tui.widgets.trace_row import TraceRow
    from textual.containers import VerticalScroll

    tid = "dock2"
    _write_task(tmp_path, tid)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "chat_completions"}
        ),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        # Индикатор — только у идущей работы: тик покоя его снял бы.
        pilot.app.active_runner = _Run(tid)
        scr.attach_active_task(tid)
        scr._show_thinking_indicator(state="thinking")
        scr._render_record(
            {
                "payload": {
                    "id": "a1",
                    "role": "assistant",
                    "content": "привет",
                }
            },
            live=True,
        )
        await pilot.pause()
        trace = pilot.app.query_one("#trace", VerticalScroll)
        assert any(
            isinstance(w, TraceRow) for w in trace.children
        )  # строка в трейсе
        assert not any(
            isinstance(w, ThinkingIndicator) for w in trace.children
        )  # индикатор — нет


# --- S10: бар стоит только во время работы открытого треда ------------------


def _app(tmp_path):
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "chat_completions"}
        ),
    )


class _Run:
    """Прогон треда `tid`; `seq=None` — раннер без `run_seq` (заглушка)."""

    _thread_kind = "run"

    def __init__(self, tid, *, running=True, seq=1):
        self._last_target = (tid, "cli")
        self.running = running
        if seq is not None:
            self.run_seq = seq

    def is_running(self):
        return self.running

    def stop(self):
        self.running = False

    def turn_effective_start(self):
        return None


def _bar(pilot):
    from textual.containers import Vertical

    return pilot.app.query_one("#indicator-bar", Vertical)


@pytest.mark.asyncio
async def test_bar_absent_when_idle(tmp_path):
    _write_task(tmp_path, "idle1")
    async with _app(tmp_path).run_test() as pilot:
        bar = _bar(pilot)
        assert bar.display is False
        scr = pilot.app.screen
        scr.attach_active_task("idle1")
        scr._poll_active()
        await pilot.pause()
        assert bar.display is False
        assert bar.region.height == 0


@pytest.mark.asyncio
async def test_bar_rule_on_attach(tmp_path):
    for tid in ("ra1", "ra2"):
        _write_task(tmp_path, tid)
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        bar = _bar(pilot)

        pilot.app.active_runner = _Run("ra1")  # живой прогон треда
        scr.attach_active_task("ra1", is_resume=True)
        await pilot.pause()
        assert bar.display is True
        assert bar.region.height == 2

        pilot.app.active_runner = None  # стоп: поток дорабатывает
        scr._stopping_runner = _Run("ra1")
        scr.attach_active_task("ra1", is_resume=True)
        assert bar.display is True

        # Стоп и переключение: у открытого треда прогона нет.
        scr.attach_active_task("ra2", is_resume=True)
        await pilot.pause()
        assert bar.display is False

        scr._run_pending = True  # ход запущен отсюда, тик его не видел
        scr.attach_active_task("ra2", is_resume=True)
        assert bar.display is True
        scr._run_pending = False


@pytest.mark.asyncio
async def test_bar_on_auto_created_thread(tmp_path):
    """Автосоздание треда под отправку: бар стоит с репликой."""
    _write_task(tmp_path, "auto1")
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr._prepare_for_new_run()
        scr.attach_active_task("auto1")
        await pilot.pause()
        assert _bar(pilot).display is True


@pytest.mark.asyncio
async def test_bar_rule_tolerates_runner_without_run_seq(tmp_path):
    _write_task(tmp_path, "seq1")
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task("seq1")
        pilot.app.active_runner = _Run("seq1", seq=None)
        assert scr._bar_wanted() is True
        scr._settled_run = (pilot.app.active_runner, None)
        assert scr._bar_wanted() is False
        pilot.app.active_runner = None


@pytest.mark.asyncio
async def test_trace_ceiling_follows_bar(tmp_path):
    """Потолок #trace пересчитан в том же вызове, где бар показан или
    снят: высота бара — константа, не замер."""
    _write_task(tmp_path, "ceil1")
    async with _app(tmp_path).run_test(size=(80, 24)) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task("ceil1")
        await pilot.pause()
        idle = scr._trace_max_height
        scr._run_pending = True
        scr._sync_indicator_bar()
        assert scr._trace_max_height == idle - 2
        scr._run_pending = False
        scr._sync_indicator_bar()
        assert scr._trace_max_height == idle
