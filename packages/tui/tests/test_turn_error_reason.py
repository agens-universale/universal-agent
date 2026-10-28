"""Причина ошибки хода под `✕ Ошибка` (решение владельца 7, смоук P3a).

Ход упал — под свежей строкой итога в ленте строка `└ <класс>: <текст>`
(`DIM`, S10 §4.6).
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
from gc_tui.services.stores.record_view import tui_section

_TID = "lerr0000000000000000000000000001a"


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


def _fresh_line(scr):
    from gc_tui.widgets.turn_line import TurnLine

    assert scr._thinking is None
    (line,) = scr.query(TurnLine).filter(".-latest")
    return line


def _line_text(scr) -> str:
    return _fresh_line(scr).render().plain


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
        assert _line_text(scr) == "✕ Ошибка\n  └ ReadError: boom"
        tui = tui_section(scr._store.get_record(_TID))
        assert "last_error" not in tui
        # Причина — под строкой итога, не отдельной строкой `!` в ленте.
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
        assert _line_text(scr) == "✕ Ошибка\n  └ ReadError: boom"


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
async def test_new_turn_after_failure_settles_reason_line(tmp_path):
    """Причина ушла с новым ходом — строка ошибки осела в одну строку."""
    pytest.importorskip("textual")
    _write_task(tmp_path, {"status": "failed"}, reason="ReadError: boom")
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(_TID, is_resume=True)
        await pilot.pause()
        line = _fresh_line(scr)
        assert line.region.height == 2
        scr._prepare_for_new_run()
        await pilot.pause()
        assert not line.is_fresh
        assert line.region.height == 1


_LONG = "MainModelUnavailable: " + "модель недоступна " * 8  # ~150 симв.


def _rows(text: str) -> list[str]:
    return text.split("\n")[1:]


async def _bar_display(scr):
    from textual.containers import Vertical

    return scr.query_one("#indicator-bar", Vertical).display


@pytest.mark.asyncio
async def test_long_reason_wraps_in_line_after_reopen(tmp_path):
    """Длинная причина — до трёх строк `└` у свежей строки; растёт лента,
    бара в покое нет."""
    pytest.importorskip("textual")
    _write_task(tmp_path, {"status": "failed"}, reason=_LONG)
    async with _app(tmp_path).run_test(size=(80, 30)) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(_TID, is_resume=True)
        await pilot.pause()
        line = _fresh_line(scr)
        rows = len(_rows(_line_text(scr)))
        assert 2 <= rows <= 3
        assert line.region.height == 1 + rows
        assert await _bar_display(scr) is False


@pytest.mark.asyncio
async def test_resize_follows_reason_rows(tmp_path):
    pytest.importorskip("textual")
    _write_task(tmp_path, {"status": "failed"}, reason=_LONG)
    async with _app(tmp_path).run_test(size=(200, 30)) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(_TID, is_resume=True)
        await pilot.pause()
        line = _fresh_line(scr)
        assert line.region.height == 2
        await pilot.resize_terminal(70, 30)
        await pilot.pause()
        rows = len(_rows(_line_text(scr)))
        assert rows >= 2
        assert line.region.height == 1 + rows
