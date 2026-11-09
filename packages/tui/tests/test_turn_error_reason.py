"""Причина ошибки хода у `✕ Ошибка` (решение владельца 7, смоук P3a;
спека предохранителя §2.4, ревизия 3).

Ход упал — у строки итога в ленте причина `<класс>: <текст>`: влезает —
тусклым хвостом ` · …` в той же строке; не влезает — у свежей строки
развёрнута под ней (как мысль), у осевшей свёрнута в хвост с `…`.
Причина хранится в записи итога хода (`outcome.error.message`, пишет
ядро): видна вживую и после повторного открытия треда; в мету TUI её не
копирует; новый ход её снимает."""

from __future__ import annotations

import json

import pytest
from gc_core.persistence.turn_outcome import TurnOutcome
from gc_testkit.develop_files import append_new_thread_messages
from gc_testkit.thread_writes import turn_outcome_record
from gc_testkit.tui_support import tui_settings, wait_until
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
    turn_outcome_record(str(tmp_path), "cli", _TID, outcome)


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
        assert _line_text(scr) == "✕ Ошибка · ReadError: boom"
        tui = tui_section(scr._store.get_record(_TID))
        assert "last_error" not in tui
        # Причина — в строке итога, не отдельной строкой `!` в ленте.
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
        assert _line_text(scr) == "✕ Ошибка · ReadError: boom"


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


_LONG = "MainModelUnavailable: " + "модель недоступна " * 8  # ~160 симв.


async def _bar_display(scr):
    from textual.containers import Vertical

    return scr.query_one("#indicator-bar", Vertical).display


def _body(scr):
    """Тело развёрнутой причины — соседний виджет за строкой."""
    (body,) = scr.query(".turn-reason")
    return body


def _body_rows(scr) -> int:
    return _body(scr).region.height


@pytest.mark.asyncio
async def test_new_turn_after_failure_settles_reason_line(tmp_path):
    """Новый ход — развёрнутая причина сворачивается в хвост строки."""
    pytest.importorskip("textual")
    _write_task(tmp_path, {"status": "failed"}, reason=_LONG)
    async with _app(tmp_path).run_test(size=(80, 30)) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(_TID, is_resume=True)
        await pilot.pause()
        line = _fresh_line(scr)
        assert line.expanded is True
        scr._prepare_for_new_run()
        await pilot.pause()
        assert not line.is_fresh
        assert line.expanded is False
        assert line.region.height == 1
        assert line.render().plain.endswith("…")
        assert _body(scr).display is False


@pytest.mark.asyncio
async def test_long_reason_expanded_after_reopen(tmp_path):
    """Длинная причина последнего хода после переоткрытия — развёрнута под
    строкой целиком, с `− свернуть`; растёт лента, бара в покое нет."""
    pytest.importorskip("textual")
    _write_task(tmp_path, {"status": "failed"}, reason=_LONG)
    async with _app(tmp_path).run_test(size=(80, 30)) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(_TID, is_resume=True)
        await pilot.pause()
        line = _fresh_line(scr)
        await wait_until(pilot, lambda: line.expanded, "причина развёрнута")
        await pilot.pause()
        assert _line_text(scr) == "✕ Ошибка"
        body = str(_body(scr).query_one(".expandable-text").render())
        assert " ".join(body.split()) == " ".join(_LONG.split())
        # Тело — сразу под строкой: текст в 2+ строки и `− свернуть`.
        assert _body(scr).region.y == line.region.bottom
        assert _body_rows(scr) >= 3
        assert await _bar_display(scr) is False


@pytest.mark.asyncio
async def test_resize_follows_reason_fit(tmp_path):
    """Влезла причина — хвост в строке; сузили окно — свежая строка
    разворачивается."""
    pytest.importorskip("textual")
    _write_task(tmp_path, {"status": "failed"}, reason=_LONG)
    async with _app(tmp_path).run_test(size=(200, 30)) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(_TID, is_resume=True)
        await pilot.pause()
        line = _fresh_line(scr)
        assert line.expanded is False
        assert line.region.height == 1
        await pilot.resize_terminal(70, 30)
        await pilot.pause()
        assert line.expanded is True
        assert line.region.height == 1
        assert _body_rows(scr) >= 3
