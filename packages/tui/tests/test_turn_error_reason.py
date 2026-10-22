"""Причина ошибки хода под `✕ Ошибка` (решение владельца 7, смоук P3a).

Ход упал — под строкой индикатора строка `└ <класс>: <текст>` (`DIM`).
Причина хранится в записи итога хода (`outcome.error.message`, пишет
ядро): видна вживую и после повторного открытия треда; в мету TUI её не
копирует; новый ход её снимает."""

from __future__ import annotations

import json

import pytest
from gc_core.persistence.turn_outcome import TurnOutcome
from gc_core.thread_store import (
    append_new_thread_messages,
    append_turn_outcome_record,
)
from gc_testkit.tui_support import tui_settings
from gc_tui.palette import DIM

_TID = "lerr0000000000000000000000000001a"


def _styles_of(text, fragment: str) -> list[str]:
    return [
        str(s.style)
        for s in text.spans
        if fragment in text.plain[s.start : s.end]
    ]


def test_failed_indicator_shows_reason_line():
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    ind = ThinkingIndicator(state="failed", error="ReadError: boom")
    text = ind._render_text()
    assert text.plain == "✕ Ошибка\n  └ ReadError: boom"
    assert _styles_of(text, "ReadError: boom") == [DIM]


def test_failed_indicator_without_reason_is_one_line():
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    assert ThinkingIndicator(state="failed")._render_text().plain == (
        "✕ Ошибка"
    )


def _write_task(tmp_path, tui: dict | None = None, reason: str = ""):
    """Тред; `reason` — последний ход упал с этой причиной (запись итога)."""
    meta = {"thread_id": _TID, "system_id": "cli"}
    if tui is not None:
        meta["tui"] = tui
    (tmp_path / f"meta_cli_{_TID}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    (tmp_path / f"data_cli_{_TID}.jsonl").write_text("", encoding="utf-8")
    if not reason:
        return
    append_new_thread_messages(
        str(tmp_path),
        "cli",
        _TID,
        [{"id": "u1", "role": "user", "content": "q"}],
        set(),
    )
    outcome = TurnOutcome(
        status="error",
        reason=None,
        opened_by="u1",
        started_at="2026-10-22T11:32:00Z",
        ended_at="",
        duration_sec=1.0,
        error={"type": reason.split(":")[0], "message": reason},
    )
    append_turn_outcome_record(str(tmp_path), "cli", _TID, outcome, seq=1)


def _app(tmp_path):
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )


class _Runner:
    def is_running(self):
        return False

    def stop(self):
        pass


def _indicator_text(scr) -> str:
    assert scr._thinking is not None
    return scr._thinking._render_text().plain


@pytest.mark.asyncio
async def test_live_failure_shows_reason_without_meta_copy(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.widgets.trace_row import TraceRow

    _write_task(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(_TID)
        scr._was_running = True
        pilot.app.active_runner = _Runner()
        pilot.app.last_runner_error = "ReadError: boom"
        scr._poll_active()
        await pilot.pause()
        assert _indicator_text(scr) == "✕ Ошибка\n  └ ReadError: boom"
        tui = scr._store.get_task_meta(_TID)["tui"]
        assert "last_error" not in tui
        # Причина — под индикатором, не отдельной строкой `!` в ленте.
        rows = [r for r in scr.query(TraceRow) if r.has_class("line-error")]
        assert rows == []


@pytest.mark.asyncio
async def test_reopen_restores_reason(tmp_path):
    pytest.importorskip("textual")
    _write_task(tmp_path, {"status": "failed"}, reason="ReadError: boom")
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(_TID, is_resume=True)
        await pilot.pause()
        assert _indicator_text(scr) == "✕ Ошибка\n  └ ReadError: boom"


@pytest.mark.asyncio
async def test_new_turn_clears_reason(tmp_path):
    pytest.importorskip("textual")
    _write_task(tmp_path, {"status": "failed"}, reason="ReadError: boom")
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(_TID)
        assert scr._latest_turn_outcome() is not None
        scr._prepare_for_new_run()
        # Запись упавшего хода — уже не строка последнего хода.
        assert scr._latest_turn_outcome() is None


@pytest.mark.asyncio
async def test_new_turn_after_failure_shrinks_indicator_bar(tmp_path):
    """Причина ушла с новым ходом — бар снова на строку ниже."""
    pytest.importorskip("textual")
    from textual.containers import Vertical

    _write_task(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(_TID)
        scr._show_thinking_indicator(state="failed")
        scr._set_indicator_error("ReadError: boom")
        bar = scr.query_one("#indicator-bar", Vertical)
        assert bar.styles.height.value == 4
        scr._show_thinking_indicator(state="thinking")
        assert bar.styles.height.value == 3


_LONG = "MainModelUnavailable: " + "модель недоступна " * 8  # ~150 симв.


def _rows(text: str) -> list[str]:
    return text.split("\n")[1:]


def test_long_reason_wraps_under_text_column():
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    ind = ThinkingIndicator(state="failed", error=_LONG)
    rows = _rows(ind._render_text().plain)
    assert 2 <= len(rows) <= 3
    assert rows[0].startswith("  └ ")
    assert all(r.startswith("    ") and r[4] != " " for r in rows[1:])
    assert not rows[-1].endswith("…")
    assert ind.error_rows == len(rows)


def test_reason_beyond_three_rows_is_cut():
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    ind = ThinkingIndicator(state="failed", error="слово " * 80)
    rows = _rows(ind._render_text().plain)
    assert len(rows) == 3
    assert rows[-1].endswith("…")


def test_short_reason_is_one_row():
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    ind = ThinkingIndicator(state="failed", error="ReadError: boom")
    assert ind.error_rows == 1


async def _bar_height(scr):
    from textual.containers import Vertical

    return scr.query_one("#indicator-bar", Vertical).styles.height.value


@pytest.mark.asyncio
async def test_long_reason_grows_bar_live_and_after_reopen(tmp_path):
    pytest.importorskip("textual")
    _write_task(tmp_path, {"status": "failed"}, reason=_LONG)
    async with _app(tmp_path).run_test(size=(80, 30)) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(_TID, is_resume=True)
        await pilot.pause()
        rows = scr._thinking.error_rows
        assert rows >= 2
        assert await _bar_height(scr) == 3 + rows
        scr._show_thinking_indicator(state="thinking")
        assert await _bar_height(scr) == 3


@pytest.mark.asyncio
async def test_resize_follows_reason_rows(tmp_path):
    pytest.importorskip("textual")
    _write_task(tmp_path, {"status": "failed"}, reason=_LONG)
    async with _app(tmp_path).run_test(size=(200, 30)) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(_TID, is_resume=True)
        await pilot.pause()
        assert scr._thinking.error_rows == 1
        assert await _bar_height(scr) == 4
        await pilot.resize_terminal(70, 30)
        await pilot.pause()
        rows = scr._thinking.error_rows
        assert rows >= 2
        assert await _bar_height(scr) == 3 + rows
