"""Причина ошибки хода под `✕ Ошибка` (решение владельца 7, смоук P3a).

Ход упал — под строкой индикатора строка `└ <класс>: <текст>` (`DIM`).
Причина хранится в meta треда (`tui.last_error`): видна вживую и после
повторного открытия треда; новый ход её очищает."""

from __future__ import annotations

import json

import pytest
from gc_testkit.tui_support import tui_settings
from gc_tui.palette import DIM

_TID = "lerr0000000000000000000000000001a"


class _ReadError(Exception):
    pass


def test_reason_is_class_and_first_line():
    from gc_tui.services.turn_error import describe_turn_error

    exc = _ReadError("connection reset by peer\ntraceback tail")
    assert describe_turn_error(exc) == "_ReadError: connection reset by peer"


def test_reason_without_message_is_class():
    from gc_tui.services.turn_error import describe_turn_error

    assert describe_turn_error(_ReadError()) == "_ReadError"


def test_long_reason_is_cut():
    from gc_tui.services.turn_error import (
        TURN_ERROR_LIMIT,
        describe_turn_error,
    )

    reason = describe_turn_error(_ReadError("слово " * 100))
    assert len(reason) <= TURN_ERROR_LIMIT
    assert reason.endswith("…")


def test_reason_masks_secrets():
    """Ключ в первой строке (адрес с ключом, заголовок) маскируется общим
    редактором секретов ядра — и в meta, и на экране."""
    from gc_core.security.log_redaction import MASK
    from gc_tui.services.turn_error import describe_turn_error

    key = "sk-" + "a1B2c3D4e5F6g7H8i9J0"
    exc = _ReadError(f"401 for https://api.example/v1?key={key}")
    reason = describe_turn_error(exc)
    assert key not in reason
    assert reason == f"_ReadError: 401 for https://api.example/v1?key={MASK}"


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


def _write_task(tmp_path, tui: dict | None = None):
    meta = {"thread_id": _TID, "system_id": "cli"}
    if tui is not None:
        meta["tui"] = tui
    (tmp_path / f"meta_cli_{_TID}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    (tmp_path / f"data_cli_{_TID}.jsonl").write_text("", encoding="utf-8")


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
async def test_live_failure_shows_and_stores_reason(tmp_path):
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
        assert tui["last_error"] == "ReadError: boom"
        # Причина — под индикатором, не отдельной строкой `!` в ленте.
        rows = [r for r in scr.query(TraceRow) if r.has_class("line-error")]
        assert rows == []


@pytest.mark.asyncio
async def test_reopen_restores_reason(tmp_path):
    pytest.importorskip("textual")
    _write_task(
        tmp_path,
        {
            "status": "failed",
            "last_indicator": {"kind": "failed", "elapsed": None},
            "last_error": "ReadError: boom",
        },
    )
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(_TID, is_resume=True)
        await pilot.pause()
        assert _indicator_text(scr) == "✕ Ошибка\n  └ ReadError: boom"


@pytest.mark.asyncio
async def test_new_turn_clears_reason(tmp_path):
    pytest.importorskip("textual")
    _write_task(
        tmp_path,
        {
            "status": "failed",
            "last_indicator": {"kind": "failed", "elapsed": None},
            "last_error": "ReadError: boom",
        },
    )
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(_TID)
        scr._prepare_for_new_run()
        tui = scr._store.get_task_meta(_TID)["tui"]
        assert tui["last_error"] is None


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
