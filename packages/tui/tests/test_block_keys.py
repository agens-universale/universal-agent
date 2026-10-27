"""Клавиши и клики блока вызовов (спека P4 §5; план Task 17).

Пробел на заголовке или члене — раскрыть / свернуть блок; Enter и
Ctrl+клик — экран деталей (на заголовке — последнего члена); клики по
фразам раскрывают и сворачивают; скрытое не в Tab-обходе.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("textual")

from gc_core.execution.progress import (
    QueueSubagentProgressSink,
    SubagentProgressEvent,
)
from gc_core.thread_store import append_new_thread_messages
from gc_testkit.tui_support import tui_settings
from textual.containers import VerticalScroll

from gc_tui.screens.tool_call_detail import ToolCallDetailScreen
from gc_tui.widgets import tool_call_row
from gc_tui.widgets.tool_blocks_view import HIDDEN_CLASS, BlockAction
from gc_tui.widgets.tool_call_row import ToolCallRow

TID = "blockkeys000000000000000000000001"


@pytest.fixture(autouse=True)
def steady_blink(monkeypatch):
    monkeypatch.setattr(tool_call_row, "blink_on", lambda frame: True)


class _Thread:
    def __init__(self, tmp_path):
        self.dir = str(tmp_path)
        self.seq = 0
        meta = {"thread_id": TID, "system_id": "cli", "status": "running"}
        (tmp_path / f"meta_cli_{TID}.json").write_text(
            json.dumps(meta), encoding="utf-8"
        )
        (tmp_path / f"data_cli_{TID}.jsonl").write_text("", encoding="utf-8")
        self.add({"id": "u1", "role": "user", "content": "сделай"})

    def add(self, *payloads):
        append_new_thread_messages(
            self.dir, "cli", TID, list(payloads), set(), start_seq=self.seq
        )
        self.seq += len(payloads)


def _call(call_id, name, args):
    return {
        "id": f"fc_{call_id}",
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(args),
    }


def _out(call_id, name="fs_read_file"):
    return {
        "id": f"fco_{call_id}",
        "type": "function_call_output",
        "call_id": call_id,
        "name": name,
        "output": json.dumps({"tool": name, "ok": True}),
    }


def _reads(*nums, done=True):
    out = []
    for i in nums:
        out.append(_call(f"r{i}", "fs_read_file", {"path": f"f{i}.py"}))
        if done:
            out.append(_out(f"r{i}"))
    return out


def _app(tmp_path):
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    app._subagent_progress_sink = QueueSubagentProgressSink()
    return app


async def _open(pilot):
    scr = pilot.app.screen
    scr.attach_active_task(TID, is_resume=True)
    await pilot.pause()
    return scr


async def _tail(pilot, thread, *payloads):
    thread.add(*payloads)
    pilot.app.screen._poll_trace_tail()
    await pilot.pause()


def _trace(scr):
    return scr.query_one("#trace", VerticalScroll)


def _row(scr, call_id) -> ToolCallRow:
    return next(
        r for r in _trace(scr).query(ToolCallRow) if r.call_id == call_id
    )


def _headers(scr, level=0) -> list[ToolCallRow]:
    return [
        r
        for r in _trace(scr).query(ToolCallRow)
        if r.mode == "header" and r._level == level
    ]


def _shown(scr, *call_ids) -> list[bool]:
    return [not _row(scr, c).has_class(HIDDEN_CLASS) for c in call_ids]


async def _press_on(pilot, widget, key):
    widget.focus()
    await pilot.pause()
    await pilot.press(key)
    await pilot.pause()


# --- пробел --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_space_toggles_explored(tmp_path):
    """Пробел на заголовке 5 чтений — раскрыт, ещё раз — свёрнут."""
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        await _tail(pilot, thread, *_reads(0, 1, 2, 3, 4))
        (header,) = _headers(scr)
        assert _shown(scr, "r0", "r1", "r4") == [False, False, True]
        await _press_on(pilot, header, "space")
        assert _shown(scr, "r0", "r1", "r4") == [True, True, True]
        await _press_on(pilot, header, "space")
        assert _shown(scr, "r0", "r1", "r4") == [False, False, True]


@pytest.mark.asyncio
async def test_space_on_member_toggles_its_block(tmp_path):
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        await _tail(pilot, thread, *_reads(0, 1, 2, 3, 4))
        await _press_on(pilot, _row(scr, "r4"), "space")
        assert _shown(scr, "r0", "r1") == [True, True]


@pytest.mark.asyncio
async def test_space_in_small_block_does_nothing(tmp_path):
    """Нечего скрывать — пробел ничего не делает, фраз нет."""
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        await _tail(pilot, thread, *_reads(0, 1))
        (header,) = _headers(scr)
        await _press_on(pilot, header, "space")
        assert _shown(scr, "r0", "r1") == [True, True]
        assert not [w for w in scr.query(BlockAction) if w.display]


@pytest.mark.asyncio
async def test_space_on_running_block_new_members_visible(tmp_path):
    """Пробел в идущем блоке — раскрыт; новые члены видны в раскрытом."""
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        await _tail(pilot, thread, *_reads(0, 1, 2, 3))
        await _tail(pilot, thread, *_reads(4, done=False))
        (header,) = _headers(scr)
        await _press_on(pilot, header, "space")
        assert _shown(scr, "r0", "r4") == [True, True]
        await _tail(pilot, thread, _out("r4"), *_reads(5, 6))
        assert all(_shown(scr, *(f"r{i}" for i in range(7))))


@pytest.mark.asyncio
async def test_space_on_agent_row_toggles_agent_window(tmp_path):
    """Пробел на строке `Agent` — окно его событий: все / 3 последних.
    Пять событий: одну строку окно не прячет, скрыто два."""
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        await _tail(
            pilot,
            thread,
            _call("a1", "subagent_worker", {"task": "проверь"}),
        )
        for i in range(5):
            _emit(pilot, "a1", f"шаг {i}")
        scr._drain_subagent_progress()
        await pilot.pause()
        prose = [
            w for w in _trace(scr).children if w.has_class("nested-prose")
        ]
        assert [w.has_class(HIDDEN_CLASS) for w in prose] == [
            True, True, False, False, False,
        ]  # fmt: skip
        await _press_on(pilot, _row(scr, "a1"), "space")
        assert not any(w.has_class(HIDDEN_CLASS) for w in prose)
        await _press_on(pilot, _row(scr, "a1"), "space")
        assert prose[0].has_class(HIDDEN_CLASS)
        assert prose[1].has_class(HIDDEN_CLASS)


def _emit(pilot, pid, prose):
    pilot.app._subagent_progress_sink.emit(
        SubagentProgressEvent(
            parent_call_id=pid,
            child_run_id="c",
            kind="intermediate_reply",
            step=1,
            max_steps=9,
            elapsed_ms=0,
            prose=prose,
        )
    )


def _nested_reads(pilot, pid, nums):
    for i in nums:
        for kind in ("tool_start", "tool_end"):
            pilot.app._subagent_progress_sink.emit(
                SubagentProgressEvent(
                    parent_call_id=pid,
                    child_run_id="c",
                    kind=kind,
                    step=1,
                    max_steps=9,
                    elapsed_ms=0,
                    tool_name="fs_read_file",
                    tool_call_id=f"n{i}",
                    args_summary=f"g{i}.py",
                    ok=True if kind == "tool_end" else None,
                )
            )


@pytest.mark.asyncio
async def test_nested_block_toggles_independently(tmp_path):
    """Одинаковый `block:0` у верхнего уровня и у субагента: пробел на
    вложенном заголовке раскрывает только вложенный блок."""
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 60)) as pilot:
        scr = await _open(pilot)
        await _tail(pilot, thread, *_reads(0, 1, 2, 3, 4))
        await _tail(
            pilot,
            thread,
            _call("a1", "subagent_worker", {"task": "проверь"}),
        )
        _nested_reads(pilot, "a1", range(5))
        scr._drain_subagent_progress()
        await pilot.pause()
        (nested,) = _headers(scr, level=1)
        assert nested.block_key == ("a1", "block:0")
        await _press_on(pilot, nested, "space")
        assert _shown(scr, "n0", "n1") == [True, True]
        assert _shown(scr, "r0", "r1") == [False, False]


# --- Enter, клики -----------------------------------------------------------


@pytest.mark.asyncio
async def test_enter_on_header_opens_last_member(tmp_path):
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        await _tail(pilot, thread, *_reads(0, 1, 2, 3, 4))
        (header,) = _headers(scr)
        await _press_on(pilot, header, "enter")
        assert isinstance(pilot.app.screen, ToolCallDetailScreen)
        assert pilot.app.screen._call_event.call_id == "r4"


@pytest.mark.asyncio
async def test_enter_on_member_opens_its_screen(tmp_path):
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        await _tail(pilot, thread, *_reads(0, 1, 2, 3, 4))
        await _press_on(pilot, _row(scr, "r3"), "enter")
        assert pilot.app.screen._call_event.call_id == "r3"


@pytest.mark.asyncio
async def test_ctrl_click_opens_detail(tmp_path):
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        await _tail(pilot, thread, *_reads(0, 1, 2, 3, 4))
        await pilot.click(_row(scr, "r3"), control=True)
        await pilot.pause()
        assert pilot.app.screen._call_event.call_id == "r3"


@pytest.mark.asyncio
async def test_click_phrases_expand_and_collapse(tmp_path):
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        await _tail(pilot, thread, *_reads(0, 1, 2, 3, 4))
        (more,) = [w for w in scr.query(BlockAction) if w.display]
        assert more.phrase == "… ещё 2"
        await pilot.click(more)
        await pilot.pause()
        assert _shown(scr, "r0", "r1") == [True, True]
        (collapse,) = [
            w
            for w in scr.query(BlockAction)
            if w.display and not w.has_class(HIDDEN_CLASS)
        ]
        assert collapse.phrase == "− свернуть группу"
        await pilot.click(collapse)
        await pilot.pause()
        assert _shown(scr, "r0", "r1") == [False, False]


# --- фокус и Tab -------------------------------------------------------------


@pytest.mark.asyncio
async def test_collapse_with_focus_on_hidden_member_moves_to_header(
    tmp_path,
):
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        await _tail(pilot, thread, *_reads(0, 1, 2, 3, 4))
        (header,) = _headers(scr)
        await _press_on(pilot, header, "space")
        await _press_on(pilot, _row(scr, "r0"), "space")
        assert _row(scr, "r0").has_class(HIDDEN_CLASS)
        assert pilot.app.focused is header


@pytest.mark.asyncio
async def test_tab_skips_hidden_rows_and_phrases(tmp_path):
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        await _tail(pilot, thread, *_reads(0, 1, 2, 3, 4))
        (header,) = _headers(scr)
        header.focus()
        await pilot.pause()
        seen = []
        for _ in range(4):
            await pilot.press("tab")
            await pilot.pause()
            seen.append(pilot.app.focused)
        assert not [w for w in seen if isinstance(w, BlockAction)]
        assert _row(scr, "r0") not in seen
        assert _row(scr, "r1") not in seen
        assert seen[:3] == [_row(scr, f"r{i}") for i in (2, 3, 4)]


@pytest.mark.asyncio
async def test_space_at_bottom_keeps_trace_at_bottom(tmp_path):
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 16)) as pilot:
        scr = await _open(pilot)
        await _tail(pilot, thread, *_reads(*range(12)))
        trace = _trace(scr)
        trace.scroll_end(animate=False)
        await pilot.pause()
        (header,) = _headers(scr)
        await _press_on(pilot, _row(scr, "r11"), "space")
        await pilot.pause()
        assert _shown(scr, "r0") == [True]
        assert trace.scroll_y == trace.max_scroll_y
        assert header is not None
