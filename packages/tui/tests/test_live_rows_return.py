"""Возврат на экран (П6): идущая строка, пропустившая кадры за экраном,
на первом же кадре после раскладки показывает текущее состояние."""

import json
from pathlib import Path

import pytest
from gc_testkit.tui_support import tui_settings
from gc_tui import textual_compat
from gc_tui.services.trace_glyphs import blink_on
from gc_tui.widgets.start_header import StartHeader
from gc_tui.widgets.thought_row import ThoughtRow
from gc_tui.widgets.tool_call_row import ToolCallRow
from textual.containers import VerticalScroll

_TID = "lr1"


def _write_task(tmp_path, records=()) -> None:
    meta = {
        "thread_id": _TID,
        "status": "running",
        "updated_at": "2026-06-15T12:00:00+00:00",
        "messages_count": len(records),
    }
    (Path(tmp_path) / f"meta_cli_{_TID}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    lines = "".join(json.dumps(r) + "\n" for r in records)
    (Path(tmp_path) / f"data_cli_{_TID}.jsonl").write_text(
        lines, encoding="utf-8"
    )


def _app(tmp_path):
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "chat_completions"}
        ),
    )


async def _session(pilot, *widgets):
    """Открытая сессия: `widgets` вверху ленты, под ними длинный ответ —
    лента у низа, виджеты за экраном."""
    scr = pilot.app.screen
    scr.attach_active_task(_TID)
    await pilot.pause()
    if widgets:
        await scr.query_one("#trace").mount_all(widgets)
    big = "\n".join(f"строка {i}" for i in range(80))
    scr._render_record(
        {"payload": {"id": "a1", "role": "assistant", "content": big}},
        live=True,
    )
    await pilot.pause(0.4)
    return scr


def _live_row() -> ToolCallRow:
    return ToolCallRow(call_id="c1", label="Run", target="sleep")


def _miss_frames(row: ToolCallRow) -> None:
    """За экраном прошло 27 с и тики до кадра, где глиф погашен; дальше
    тики молчат."""
    row.set_clock_source(lambda _cid: 27.0)
    while blink_on(row._pulse_frame):
        row._pulse_tick()
    row.refresh_running_elapsed()
    row._pulse_timer.stop()
    row._pulse_timer = None
    row.refresh_running_elapsed = lambda: None


async def _scroll_home(pilot, scr) -> None:
    scr._stick_bottom = False
    scr.query_one("#trace", VerticalScroll).scroll_home(animate=False)
    await pilot.pause()


def _spy(monkeypatch, widget, name: str) -> list:
    calls = []
    monkeypatch.setattr(widget, name, lambda *a, **k: calls.append(a))
    return calls


@pytest.mark.asyncio
async def test_row_redraws_on_first_frame_after_scroll_back(tmp_path):
    _write_task(tmp_path)
    row = _live_row()
    async with _app(tmp_path).run_test(size=(80, 24)) as pilot:
        scr = await _session(pilot, row)
        assert textual_compat.is_visible(row._content_static) is False
        _miss_frames(row)
        assert row._frame_missed is True
        assert row in scr._missed_frames
        stale = row._content_static.content.plain
        assert "27с" not in stale
        await _scroll_home(pilot, scr)
        assert "· 27с" in row._content_static.content.plain
        assert row._prefix_static.content.plain == row._render_prefix().plain
        assert row._prefix_static.content.plain.strip() == ""
        assert row._frame_missed is False
        assert row not in scr._missed_frames


@pytest.mark.asyncio
async def test_repeated_signal_does_not_redraw(tmp_path, monkeypatch):
    _write_task(tmp_path)
    row = _live_row()
    async with _app(tmp_path).run_test(size=(80, 24)) as pilot:
        scr = await _session(pilot, row)
        _miss_frames(row)
        await _scroll_home(pilot, scr)
        calls = _spy(monkeypatch, row, "_update")
        scr.screen_layout_refresh_signal.publish(scr)
        await pilot.pause()
        assert calls == []


def _thought() -> ThoughtRow:
    return ThoughtRow(text="думаю", live=True, attempt=None, seconds=1.0)


@pytest.mark.asyncio
async def test_handler_skips_invisible_and_unmarked(tmp_path, monkeypatch):
    _write_task(tmp_path)
    row, thought = _live_row(), _thought()
    header = StartHeader(mcp_total=2, id="hdr2")
    async with _app(tmp_path).run_test(size=(80, 24)) as pilot:
        scr = await _session(pilot, row, thought, header)
        widgets = {row: "_update", thought: "_update", header: "_redraw"}
        for widget in widgets:
            assert textual_compat.is_visible(widget) is False
            scr.note_missed_frame(widget)
            widget._frame_missed = True
        spies = [_spy(monkeypatch, w, name) for w, name in widgets.items()]
        scr._on_layout_refreshed(scr)
        assert spies == [[], [], []]
        assert set(scr._missed_frames) == set(widgets)

        for widget in widgets:
            widget._frame_missed = False
        await _scroll_home(pilot, scr)
        assert all(textual_compat.is_visible(w) for w in widgets)
        scr._on_layout_refreshed(scr)
        assert spies == [[], [], []]
        assert [w.redraw_if_missed() for w in widgets] == [False] * 3

        for widget in widgets:
            widget._frame_missed = True
        scr._on_layout_refreshed(scr)
        assert [len(calls) for calls in spies] == [1, 1, 1]
        assert set(scr._missed_frames) == set()


@pytest.mark.asyncio
async def test_handler_drops_removed_rows(tmp_path):
    _write_task(tmp_path)
    row = _live_row()
    async with _app(tmp_path).run_test(size=(80, 24)) as pilot:
        scr = await _session(pilot, row)
        _miss_frames(row)
        assert row in scr._missed_frames
        await row.remove()
        scr._on_layout_refreshed(scr)
        assert row not in scr._missed_frames


@pytest.mark.asyncio
async def test_handler_idle_during_select(tmp_path):
    _write_task(tmp_path)
    row = _live_row()
    async with _app(tmp_path).run_test(size=(80, 24)) as pilot:
        scr = await _session(pilot, row)
        _miss_frames(row)
        scr._selecting = True
        await _scroll_home(pilot, scr)
        assert textual_compat.is_visible(row._content_static) is True
        assert "27с" not in row._content_static.content.plain
        assert row in scr._missed_frames

        scr._selecting = False
        scr.screen_layout_refresh_signal.publish(scr)
        await pilot.pause()
        assert "· 27с" in row._content_static.content.plain
        assert row not in scr._missed_frames


def _read_call_records() -> list[dict]:
    """Ход с идущим чтением: член блока `Explored` и его заголовок."""
    return [
        {
            "type": "message",
            "seq": 1,
            "ts_utc": "2026-06-15T12:00:00+00:00",
            "payload": {"id": "u1", "role": "user", "content": "go"},
        },
        {
            "type": "message",
            "seq": 2,
            "ts_utc": "2026-06-15T12:00:01+00:00",
            "payload": {
                "id": "fc1",
                "type": "function_call",
                "call_id": "c1",
                "name": "fs_read_file",
                "arguments": json.dumps({"path": "src/x.py"}),
            },
        },
    ]


@pytest.mark.asyncio
async def test_group_header_row_refreshed(tmp_path):
    _write_task(tmp_path, _read_call_records())
    async with _app(tmp_path).run_test(size=(80, 24)) as pilot:
        scr = await _session(pilot)
        header = next(
            r for r in pilot.app.query(ToolCallRow) if r.mode == "header"
        )
        assert header._blinking is True
        assert textual_compat.is_visible(header._content_static) is False
        while blink_on(header._pulse_frame):
            header._pulse_tick()
        header._pulse_timer.stop()
        header._pulse_timer = None
        assert header in scr._missed_frames
        assert header._prefix_static.content.plain.strip() != ""
        await _scroll_home(pilot, scr)
        assert header._prefix_static.content.plain.strip() == ""
        assert header._frame_missed is False
        assert header not in scr._missed_frames
