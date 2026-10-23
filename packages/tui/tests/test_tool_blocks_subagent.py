"""Блок субагента в живой ленте (спека P4 §7; план Task 15).

Один сценарий субагента даёт и события прогресса ядра (живая лента), и
записи sidecar (возврат в сессию): оба пути рисуют одно. Каждый кадр
проходит сетку колонок `assert_trace_grid` (вложенный глиф — в `c + 2`).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("textual")

from gc_core.execution.progress import (
    QueueSubagentProgressSink,
    SubagentProgressEvent,
    parse_tool_result_status,
    result_facts,
    tool_args_summary,
)
from gc_core.persistence.turn_outcome import USER_STOP, TurnOutcome
from gc_core.thread_store import (
    append_new_thread_messages,
    append_subagent_trace_record,
    append_turn_outcome_record,
)
from gc_testkit.trace_grid import assert_trace_grid, trace_rows
from gc_testkit.tui_support import tui_settings
from textual.containers import VerticalScroll

from gc_tui.widgets import tool_call_row
from gc_tui.widgets.tool_blocks_view import BlockAction
from gc_tui.widgets.tool_call_row import ToolCallRow
from gc_tui.widgets.turn_line import TurnLine

TID = "agentblk0000000000000000000000001"
ORIGIN = 1
AGENT = "sub1"


@pytest.fixture(autouse=True)
def steady_rows(monkeypatch):
    """Живой глиф виден в любом кадре, часы строк стоят."""
    monkeypatch.setattr(tool_call_row, "blink_on", lambda frame: True)
    monkeypatch.setattr(
        tool_call_row, "time", SimpleNamespace(monotonic=lambda: 100.0)
    )


# --- сценарий субагента -----------------------------------------------------


def _read(i: int) -> tuple:
    return ("call", f"n{i}", "fs_read_file", {"path": f"client/m{i}.py"})


def _ran(i: int) -> tuple:
    return ("call", f"x{i}", "exec_command", {"command": "uv run pytest -q"})


def _p418() -> list[tuple]:
    """Макет `p4-18-agent`: проза, `Explored` из 4 чтений, `Ran`, проза."""
    return [
        ("prose", "Смотрю, где клиент берёт таймаут."),
        *[_read(i) for i in range(4)],
        _ran(0),
        ("prose", "Клиент читает таймаут из конфига."),
    ]


def _output(name: str) -> str:
    """Вывод вызова — конверт тула без сводки: хвоста у строки нет."""
    return json.dumps({"tool": name, "ok": True})


def _progress(steps: list[tuple], pid: str = AGENT) -> list:
    base = dict(parent_call_id=pid, child_run_id="c", step=1, max_steps=9)
    out = []
    for step in steps:
        if step[0] == "prose":
            out.append(
                SubagentProgressEvent(
                    kind="intermediate_reply",
                    elapsed_ms=0,
                    prose=step[1],
                    **base,
                )
            )
            continue
        if step[0] == "user":
            out.append(
                SubagentProgressEvent(
                    kind="user_message", elapsed_ms=0, prose=step[1], **base
                )
            )
            continue
        _kind, call_id, name, args = step
        summary = tool_args_summary(name, json.dumps(args))
        for kind in ("tool_start", "tool_end"):
            out.append(
                SubagentProgressEvent(
                    kind=kind,
                    elapsed_ms=0,
                    tool_name=name,
                    tool_call_id=call_id,
                    args_summary=summary,
                    ok=True if kind == "tool_end" else None,
                    output=_output(name) if kind == "tool_end" else None,
                    **base,
                )
            )
    return out


def _sidecar(tmp_path, steps: list[tuple], pid: str = AGENT) -> None:
    for step in steps:
        if step[0] == "prose":
            records = [("intermediate_reply", {"prose": step[1]})]
        elif step[0] == "user":
            records = [("user", {"content": step[1]})]
        else:
            _kind, call_id, name, args = step
            records = [
                (
                    "call",
                    {
                        "type": "function_call",
                        "name": name,
                        "arguments": json.dumps(args),
                        "call_id": call_id,
                    },
                ),
                (
                    "result",
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": _output(name),
                    },
                ),
            ]
        for kind, payload in records:
            append_subagent_trace_record(
                str(tmp_path),
                "cli",
                TID,
                parent_call_id=pid,
                child_run_id="c",
                kind=kind,
                payload=payload,
            )


def _call_ids(steps: list[tuple]) -> list[str]:
    return [s[1] for s in steps if s[0] == "call"]


# --- тред родителя -----------------------------------------------------------


class _Thread:
    """Писатель треда родителя, как ядро."""

    def __init__(self, tmp_path, *, status="running"):
        self.dir = str(tmp_path)
        self.seq = 0
        meta = {"thread_id": TID, "system_id": "cli", "status": status}
        (tmp_path / f"meta_cli_{TID}.json").write_text(
            json.dumps(meta), encoding="utf-8"
        )
        (tmp_path / f"data_cli_{TID}.jsonl").write_text("", encoding="utf-8")
        self.add({"id": "u1", "role": "user", "content": "проверь клиент"})

    def add(self, *payloads):
        append_new_thread_messages(
            self.dir, "cli", TID, list(payloads), set(), start_seq=self.seq
        )
        self.seq += len(payloads)

    def record(self, calls, *, stopped=False):
        outcome = TurnOutcome(
            status="stopped" if stopped else "done",
            reason=USER_STOP if stopped else None,
            opened_by="u1",
            started_at="2026-10-22T11:30:00Z",
            ended_at="2026-10-22T11:30:12Z",
            duration_sec=12.0,
            error=None,
            calls=calls,
        )
        append_turn_outcome_record(self.dir, "cli", TID, outcome, seq=self.seq)


def _agent_call(pid: str = AGENT) -> dict:
    return {
        "id": f"fc_{pid}",
        "type": "function_call",
        "call_id": pid,
        "name": "subagent_worker",
        "arguments": json.dumps({"task": "проверка клиента"}),
    }


def _agent_done(pid: str = AGENT) -> dict:
    return {
        "id": f"fco_{pid}",
        "type": "function_call_output",
        "call_id": pid,
        "name": "subagent_worker",
        "output": "{}",
    }


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


async def _emit(pilot, events):
    for ev in events:
        pilot.app._subagent_progress_sink.emit(ev)
    pilot.app.screen._drain_subagent_progress()
    await pilot.pause()


def _rows(pilot) -> list[str]:
    """Кадр ленты от реплики пользователя — через сетку колонок."""
    rows = trace_rows(pilot.app, exclude=(TurnLine,))
    start = next(i for i, r in enumerate(rows) if r.plain.startswith(" ›"))
    rows = rows[start:]
    assert_trace_grid(rows, origin=ORIGIN)
    return [r.plain[ORIGIN:].rstrip() for r in rows if r.plain.strip()]


def _agent_rows(lines: list[str]) -> list[str]:
    """Строки блока `Agent`: от его строки до первой строки уровня 0."""
    at = next(i for i, line in enumerate(lines) if "Agent  " in line)
    block = [lines[at]]
    for line in lines[at + 1 :]:
        if not line.startswith("  "):
            break
        block.append(line)
    return block


async def _live(pilot, tmp_path, steps, *, calls):
    thread = _Thread(tmp_path)
    await _open(pilot)
    await _tail(pilot, thread, _agent_call())
    await _emit(pilot, _progress(steps))
    await _tail(pilot, thread, _agent_done())
    thread.record(calls)
    pilot.app.screen._poll_trace_tail()
    await pilot.pause()
    return _rows(pilot)


def _write_reopen(tmp_path, steps, *, calls) -> None:
    thread = _Thread(tmp_path, status="stopped")
    thread.add(_agent_call(), _agent_done())
    thread.record(calls)
    _sidecar(tmp_path, steps)


# --- макет p4-18 --------------------------------------------------------------


@pytest.mark.asyncio
async def test_p418_live_equals_reopen(tmp_path):
    """`p4-18-agent`: вживую, после возврата и в превью — один список
    строк; окно 3 последних события, `└ … ещё 1` на +2 от глифа `Agent`."""
    steps = _p418()
    calls = {cid: 0.0 for cid in [AGENT, *_call_ids(steps)]}
    live_dir, reopen_dir = tmp_path / "live", tmp_path / "reopen"
    live_dir.mkdir()
    reopen_dir.mkdir()
    async with _app(live_dir).run_test(size=(100, 40)) as pilot:
        live = await _live(pilot, live_dir, steps, calls=calls)
    _write_reopen(reopen_dir, steps, calls=calls)
    async with _app(reopen_dir).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        reopen = _rows(pilot)
        await _push_preview(pilot, scr)
        preview = _rows(pilot)
    assert live == reopen == preview
    assert _agent_rows(live) == [
        "● Agent  проверка клиента · 0с",
        "  └ … ещё 1",
        "  ● Explored",
        "    └ … ещё 1",
        "      Read  client/m1.py · 0с",
        "      Read  client/m2.py · 0с",
        "      Read  client/m3.py · 0с",
        "  ● Ran  uv run pytest -q · 0с",
        "  ● Клиент читает таймаут из конфига.",
    ]


@pytest.mark.asyncio
async def test_agent_window_expands_on_click(tmp_path):
    """Клик по `… ещё N` окна `Agent` — видны все события, маркер ушёл,
    `− свернуть` — последней строкой блока `Agent` в колонке 4 (одна фраза
    — последняя строка блока); клик по ней — снова окно."""
    steps = _p418()
    _write_reopen(tmp_path, steps, calls={})
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        (phrase,) = _window_phrases(scr, "top")
        assert phrase.phrase == "… ещё 1"
        await pilot.click(phrase)
        await pilot.pause()
        lines = _agent_rows(_rows(pilot))
        assert lines[1] == "  ● Смотрю, где клиент берёт таймаут."
        assert lines[-1] == "    − свернуть"
        assert not any("└ − свернуть" in line for line in lines)
        (collapse,) = _window_phrases(scr, "bottom")
        await pilot.click(collapse)
        await pilot.pause()
        lines = _agent_rows(_rows(pilot))
        assert lines[1] == "  └ … ещё 1"
        assert "  ● Смотрю, где клиент берёт таймаут." not in lines
        assert not any("свернуть" in line for line in lines)


def _window_phrases(scr, slot: str) -> list:
    return [
        w
        for w in scr.query(BlockAction)
        if w.level == AGENT and w.key == f"agent:{slot}"
    ]


@pytest.mark.asyncio
async def test_agent_collapse_stays_last_on_new_event(tmp_path):
    """Окно раскрыто, субагент идёт: новое событие встаёт над
    `− свернуть` — фраза остаётся последней строкой блока `Agent`."""
    steps = _p418()
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        thread = _Thread(tmp_path)
        scr = await _open(pilot)
        await _tail(pilot, thread, _agent_call())
        await _emit(pilot, _progress(steps))
        (phrase,) = _window_phrases(scr, "top")
        await pilot.click(phrase)
        await pilot.pause()
        await _emit(pilot, _progress([("prose", "Ещё одно событие.")]))
        lines = _agent_rows(_rows(pilot))
        assert lines[-2:] == ["  ● Ещё одно событие.", "    − свернуть"]


@pytest.mark.asyncio
async def test_nested_order_matches_preview(tmp_path):
    """Идущий субагент: `Explored` из 5 чтений, затем `Ran`, затем
    вложенная реплика — порядок виджетов в ленте тот же, что после
    возврата (§9.1): хвостовой якорь — последний виджет уровня."""
    steps = [
        *[_read(i) for i in range(5)],
        _ran(0),
        ("user", "и тесты"),
    ]
    calls = {cid: 0.0 for cid in [AGENT, *_call_ids(steps)]}
    live_dir, reopen_dir = tmp_path / "live", tmp_path / "reopen"
    live_dir.mkdir()
    reopen_dir.mkdir()
    async with _app(live_dir).run_test(size=(100, 40)) as pilot:
        thread = _Thread(live_dir)
        await _open(pilot)
        await _tail(pilot, thread, _agent_call())
        await _emit(pilot, _progress(steps))
        live = _rows(pilot)
        live_order = _order(pilot)
    _write_reopen(reopen_dir, steps, calls=calls)
    async with _app(reopen_dir).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        reopen_order = _order(pilot)
        await _push_preview(pilot, scr)
        preview_order = _order(pilot)
    assert live_order == reopen_order == preview_order
    assert _agent_rows(live)[1:] == [
        "  ● Explored",
        "    └ … ещё 2",
        "      Read  client/m2.py · 0с",
        "      Read  client/m3.py · 0с",
        "      Read  client/m4.py · 0с",
        "  ● Ran  uv run pytest -q · 0с",
        "  › и тесты",
    ]


async def _push_preview(pilot, scr) -> None:
    from gc_tui.screens.preview import PreviewScreen

    await pilot.app.push_screen(
        PreviewScreen(
            segments=scr._store.read_segments(TID),
            thread_id=TID,
            project_path="",
            subagent_records=scr._store.read_subagent_trace(TID),
        )
    )
    await pilot.pause()


def _order(pilot) -> list[str]:
    """Порядок видимых вложенных виджетов: ключ строки или тип."""
    trace = pilot.app.screen.query_one("#trace", VerticalScroll)
    out = []
    for w in trace.children:
        if not w.has_class("-in-agent") or not w.display:
            continue
        if w.has_class("-block-hidden"):
            continue
        if isinstance(w, ToolCallRow):
            out.append(w.mode if w.mode == "header" else w.call_id)
        else:
            out.append(type(w).__name__)
    return out


@pytest.mark.asyncio
async def test_no_blank_lines_inside_agent(tmp_path):
    """Внутри блока субагента пустых строк нет (§7), над `Agent` — есть."""
    steps = [
        ("prose", "Смотрю."),
        _read(0),
        ("user", "и тесты"),
        _ran(0),
    ]
    _write_reopen(tmp_path, steps, calls={})
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        await _open(pilot)
        rows = [
            r.plain[ORIGIN:].rstrip()
            for r in trace_rows(pilot.app, exclude=(TurnLine,))
        ]
        at = next(i for i, line in enumerate(rows) if "Agent  " in line)
        assert rows[at - 1] == ""
        inner = rows[at + 1 : at + 6]
        assert all(line.startswith("  ") for line in inner), inner


@pytest.mark.asyncio
async def test_parallel_subagents_each_own_block(tmp_path):
    """Два параллельных субагента: вложенные встают в свой блок."""
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        await _tail(pilot, thread, _agent_call("a1"), _agent_call("a2"))
        await _emit(pilot, _progress([_read(0)], pid="a1"))
        await _emit(pilot, _progress([_read(1)], pid="a2"))
        await _emit(pilot, _progress([_read(2)], pid="a1"))
        lines = _rows(pilot)
        a1 = scr._block_views["a1"].model
        a2 = scr._block_views["a2"].model
        assert a1.block_of("n0") == a1.block_of("n2")
        assert a2.block_of("n1") is not None
        assert a1.block_of("n1") is None
        first = lines.index(next(line for line in lines if "Agent  " in line))
        second = next(
            i
            for i, line in enumerate(lines)
            if "Agent  " in line and i > first
        )
        assert "client/m0.py" in "".join(lines[first:second])
        assert "client/m2.py" in "".join(lines[first:second])
        assert "client/m1.py" in "".join(lines[second:])


@pytest.mark.asyncio
async def test_outside_retention_agent_without_nested(tmp_path):
    """Вне окна retention вложенные не монтируются — `Agent` без них."""
    from gc_tui.screens import main as main_module

    _write_reopen(tmp_path, [_read(0)], calls={})
    original = main_module.parent_outside_retention
    try:
        main_module.parent_outside_retention = lambda *_a: True
        async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
            scr = await _open(pilot)
            assert AGENT not in scr._block_views
            assert not [
                w for w in scr.query(ToolCallRow) if w.has_class("-in-agent")
            ]
    finally:
        main_module.parent_outside_retention = original


@pytest.mark.asyncio
async def test_reopen_two_turns_nested_timers(tmp_path):
    """Тред из двух ходов, в каждом свой субагент: после возврата у
    вложенных строк — застывшие таймеры из `calls` своего хода."""
    thread = _Thread(tmp_path, status="stopped")
    thread.add(_agent_call("a1"), _agent_done("a1"))
    thread.record({"a1": 3.0, "n0": 1.0})
    thread.add({"id": "u2", "role": "user", "content": "ещё"})
    thread.add(_agent_call("a2"), _agent_done("a2"))
    thread.record({"a2": 5.0, "n1": 2.0})
    _sidecar(tmp_path, [_read(0)], pid="a1")
    _sidecar(tmp_path, [_read(1)], pid="a2")
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        await _open(pilot)
        lines = _rows(pilot)
    assert "    └ Read  client/m0.py · 1с" in lines
    assert "    └ Read  client/m1.py · 2с" in lines


# --- стоп посреди вложенного блока -------------------------------------------

_CANCELLED = json.dumps(
    {
        "ok": False,
        "status": "cancelled_by_user",
        "error": {"code": "cancelled_by_user", "message": "stop"},
    }
)


def _cancelled_progress(call_id: str) -> list:
    """Вложенное чтение, оборванное стопом: событие прогресса ядра."""
    ok, brief = parse_tool_result_status(_CANCELLED)
    status, fields = result_facts(_CANCELLED)
    base = dict(
        parent_call_id=AGENT, child_run_id="c", step=1, max_steps=9,
        elapsed_ms=0, tool_name="fs_read_file", tool_call_id=call_id,
    )  # fmt: skip
    summary = tool_args_summary(
        "fs_read_file", json.dumps({"path": "client/m9.py"})
    )
    return [
        SubagentProgressEvent(kind="tool_start", args_summary=summary, **base),
        SubagentProgressEvent(
            kind="tool_end",
            ok=ok,
            error_brief=brief,
            result_status=status,
            result_error=fields,
            output=_CANCELLED,
            **base,
        ),
    ]


def _cancelled_sidecar(tmp_path, call_id: str) -> None:
    call = {
        "type": "function_call",
        "name": "fs_read_file",
        "arguments": json.dumps({"path": "client/m9.py"}),
        "call_id": call_id,
    }
    end = {"call_id": call_id, "output": _CANCELLED}
    for kind, payload in (("call", call), ("error", end)):
        append_subagent_trace_record(
            str(tmp_path),
            "cli",
            TID,
            parent_call_id=AGENT,
            child_run_id="c",
            kind=kind,
            payload=payload,
        )


@pytest.mark.asyncio
async def test_stop_mid_nested_explored_live_reopen_preview(tmp_path):
    """Стоп посреди вложенного `Explored`: исход хода родителя (стоп)
    доходит и до уровня субагента — `○ Explored` вживую, после возврата
    и в превью (уровень субагента домонтирован после записи итога)."""
    steps = [_read(i) for i in range(2)]
    calls = {cid: 0.0 for cid in [AGENT, *_call_ids(steps)]}
    live_dir, reopen_dir = tmp_path / "live", tmp_path / "reopen"
    live_dir.mkdir()
    reopen_dir.mkdir()
    async with _app(live_dir).run_test(size=(100, 40)) as pilot:
        thread = _Thread(live_dir)
        await _open(pilot)
        await _tail(pilot, thread, _agent_call())
        await _emit(pilot, _progress(steps) + _cancelled_progress("n9"))
        await _tail(pilot, thread, _agent_done())
        thread.record(calls, stopped=True)
        pilot.app.screen._poll_trace_tail()
        await pilot.pause()
        live = _agent_rows(_rows(pilot))
    thread = _Thread(reopen_dir, status="stopped")
    thread.add(_agent_call(), _agent_done())
    thread.record(calls, stopped=True)
    _sidecar(reopen_dir, steps)
    _cancelled_sidecar(reopen_dir, "n9")
    async with _app(reopen_dir).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        reopen = _agent_rows(_rows(pilot))
        await _push_preview(pilot, scr)
        preview = _agent_rows(_rows(pilot))
    assert live[1:] == [
        "  ○ Explored",
        "    └ Read  client/m0.py · 0с",
        "      Read  client/m1.py · 0с",
        "      Read  client/m9.py · 0с",
        "        └ отменено",
    ]
    # Остановленный посреди вызов после переоткрытия — без таймера: в
    # `calls` его нет (спека §3.4, «Непокрыто»); остальное — одно.
    assert reopen == preview
    assert reopen == [line.replace("m9.py · 0с", "m9.py") for line in live]


@pytest.mark.asyncio
async def test_stopped_nested_orphan_call_reopen_preview(tmp_path):
    """Стоп, вложенный вызов без исхода: уровень домонтирован после
    записи итога — член оборвался (`└ отменено`), `○ Explored`; после
    возврата и в превью одинаково (§2.2, §7)."""
    thread = _Thread(tmp_path, status="stopped")
    thread.add(_agent_call(), _agent_done())
    thread.record({}, stopped=True)
    _sidecar(tmp_path, [_read(0)])
    call = {
        "type": "function_call",
        "name": "fs_read_file",
        "arguments": json.dumps({"path": "client/m9.py"}),
        "call_id": "n9",
    }
    append_subagent_trace_record(
        str(tmp_path),
        "cli",
        TID,
        parent_call_id=AGENT,
        child_run_id="c",
        kind="call",
        payload=call,
    )
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        reopen = _agent_rows(_rows(pilot))
        await _push_preview(pilot, scr)
        preview = _agent_rows(_rows(pilot))
    assert reopen[1:] == [
        "  ○ Explored",
        "    └ Read  client/m0.py",
        "      Read  client/m9.py",
        "        └ отменено",
    ]
    assert preview == reopen
