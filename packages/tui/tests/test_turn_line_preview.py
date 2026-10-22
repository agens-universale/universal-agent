"""Строка хода одним билдером: превью `/sessions` и read-only рендер.

`build_turn_line` — один билдер для главного экрана и превью. Строка
последнего хода видна и там, и там (S10): головы и места строк превью
совпадают с переоткрытой лентой того же треда.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from gc_core.persistence.turn_outcome import TurnOutcome
from gc_testkit.tui_support import tui_settings
from gc_tui.services.relative_time import parse_iso_utc

from test_turn_line_history import (
    COMPACTED_TRACE,
    TID,
    _done_line,
    _ended,
    _hm,
    _trace_lines,
    write_compacted_thread,
)


def _outcome(**kw):
    return TurnOutcome(
        status=kw.get("status", "done"),
        reason=kw.get("reason"),
        opened_by=kw.get("opened_by", "u1"),
        started_at="2026-10-22T11:30:00Z",
        ended_at=_ended(1),
        duration_sec=12.0,
        error=kw.get("error"),
    )


def _local_end():
    return parse_iso_utc(_ended(1)).astimezone()


def test_build_turn_line_head_and_opener():
    from gc_tui.services.trace_render import build_turn_line
    from gc_tui.widgets.turn_line import TurnLine

    line = build_turn_line(_outcome(), now=_local_end())
    assert isinstance(line, TurnLine)
    assert line.text.plain == _done_line(1)
    assert line.opened_by == "u1"
    assert not line.has_class("-latest")


def test_build_turn_line_error_is_one_row_without_reason():
    from gc_tui.services.trace_render import build_turn_line

    line = build_turn_line(
        _outcome(
            status="error",
            error={"type": "ReadError", "message": "ReadError: boom"},
        ),
        now=_local_end(),
    )
    assert line.text.plain == f"✕ Ошибка · {_hm(_ended(1))}"


def test_build_turn_line_clock_from_now():
    from gc_tui.services.trace_render import build_turn_line

    line = build_turn_line(
        _outcome(status="stopped", reason="user_stop"),
        now=_local_end() + timedelta(days=1),
    )
    assert line.text.plain == f"■ Отменено · вчера {_hm(_ended(1))}"


@pytest.mark.asyncio
async def test_turn_line_copies_its_head():
    from textual.app import App
    from textual.selection import Selection

    from gc_tui.services.trace_render import build_turn_line

    line = build_turn_line(_outcome(), now=_local_end())

    class _A(App):
        def compose(self):
            yield line

    async with _A().run_test() as pilot:
        await pilot.pause()
        text, _end = line.get_selection(Selection(None, None))
        assert text == _done_line(1)


@pytest.fixture
def _pin_now(monkeypatch):
    from gc_tui.services import relative_time

    now = parse_iso_utc(_ended(9)).astimezone() + timedelta(minutes=1)
    monkeypatch.setattr(relative_time, "_now_local", lambda: now)


@pytest.mark.asyncio
async def test_preview_shows_every_turn_line_like_the_trace(
    tmp_path, _pin_now
):
    """Превью `/sessions` — те же строки и порядок, что лента, до
    последней строки треда включительно."""
    from textual.app import App

    from gc_tui.app import TuiApp
    from gc_tui.screens.preview import PreviewScreen
    from gc_tui.services.stores.file_store import FileTaskStore

    write_compacted_thread(tmp_path)
    segments = FileTaskStore(
        threads_dir=str(tmp_path), system="cli"
    ).read_segments(TID)

    class _A(App):
        def on_mount(self):
            self.push_screen(
                PreviewScreen(
                    segments=segments, thread_id=TID, project_path=""
                )
            )

    async with _A().run_test() as pilot:
        await pilot.pause()
        preview = _trace_lines(pilot.app.screen.query_one("#trace"))
    assert preview == COMPACTED_TRACE

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(TID, is_resume=True)
        await pilot.pause()
        trace = _trace_lines(scr.query_one("#trace"))
    assert preview == trace
