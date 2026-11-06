"""Группа закрывается событием ленты (спека P4 §2.2; план Task 13).

Одна точка закрытия верхнего уровня — `TraceScreen._note_top_event`: её
зовут `_trace_mount` и обе ветки рамки плана. Не закрывают: виджеты,
выданные моделью блоков, живой стрим, реплика в очереди и вложенный
виджет субагента без якоря.
"""

from __future__ import annotations

import ast
import json
import threading

import pytest

pytest.importorskip("textual")

from gc_core.persistence.turn_outcome import TurnOutcome
from gc_testkit.develop_files import append_new_thread_messages
from gc_testkit.paths import TUI_PKG
from gc_testkit.thread_writes import turn_outcome_record
from gc_testkit.tui_support import tui_settings, wait_until
from textual.containers import VerticalScroll
from textual.widgets import Static

from gc_tui.widgets.tool_blocks_view import BlockAction
from gc_tui.widgets.tool_call_row import ToolCallRow
from gc_tui.widgets.turn_line import TurnLine

TID = "closing0000000000000000000000001"


# --- тред -------------------------------------------------------------------


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

    def record(self, opened_by="u1", **kw):
        outcome = TurnOutcome(
            status=kw.get("status", "done"),
            reason=kw.get("reason"),
            opened_by=opened_by,
            started_at="2026-10-22T11:30:00Z",
            ended_at="2026-10-22T11:30:12Z",
            duration_sec=12.0,
            error=None,
            calls=kw.get("calls"),
        )
        turn_outcome_record(self.dir, "cli", TID, outcome)


def _read(i):
    return {
        "id": f"fc_r{i}",
        "type": "function_call",
        "call_id": f"r{i}",
        "name": "fs_read_file",
        "arguments": json.dumps({"path": f"f{i}.py"}),
    }


def _read_out(i, body=None):
    return {
        "id": f"fco_r{i}",
        "type": "function_call_output",
        "call_id": f"r{i}",
        "name": "fs_read_file",
        "output": json.dumps(body or {"tool": "fs_read_file", "ok": True}),
    }


def _reads(*nums):
    out = []
    for i in nums:
        out += [_read(i), _read_out(i)]
    return out


def _answer(mid, text):
    return {
        "id": mid,
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


def _app(tmp_path):
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )


async def _open(pilot):
    scr = pilot.app.screen
    scr.attach_active_task(TID, is_resume=True)
    await pilot.pause()
    return scr


async def _tail(pilot, thread, *payloads):
    thread.add(*payloads)
    pilot.app.screen._poll_trace_tail()
    await pilot.pause()


def _blocks(scr) -> list[str]:
    """Блоки ленты по порядку: заголовок или одиночная строка."""
    trace = scr.query_one("#trace", VerticalScroll)
    return [r.call_id for r in trace.query(ToolCallRow) if r.mode == "header"]


def _above_turn_line(scr):
    """Видимый виджет ленты прямо над строкой итога хода."""
    shown = [
        w
        for w in scr.query_one("#trace", VerticalScroll).children
        if w.display and not w.has_class("-block-hidden")
    ]
    at = next(i for i, w in enumerate(shown) if isinstance(w, TurnLine))
    return shown[at - 1]


def _block_of(scr, call_id) -> str:
    return scr._top_blocks().model.block_of(call_id)


# --- что закрывает ----------------------------------------------------------


@pytest.mark.asyncio
async def test_grouping_rule_prose_closes(tmp_path):
    """Промежуточная реплика агента закрывает группу."""
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        await _tail(pilot, thread, *_reads(0))
        await _tail(pilot, thread, _answer("a1", "Посмотрю ещё."))
        await _tail(pilot, thread, *_reads(1))
        assert _block_of(scr, "r0") != _block_of(scr, "r1")
        assert len(_blocks(scr)) == 2


@pytest.mark.asyncio
async def test_grouping_rule_queue_does_not_close(tmp_path):
    """Реплика в очереди группу не рвёт: новые члены встают над ней."""
    from gc_tui.widgets.user_reply import is_queued

    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        await _tail(pilot, thread, *_reads(0))
        reply = scr._mount_user_reply("ещё", state="queued")
        await pilot.pause()
        assert is_queued(reply)
        await _tail(pilot, thread, *_reads(1))
        assert _block_of(scr, "r0") == _block_of(scr, "r1")
        trace = scr.query_one("#trace", VerticalScroll)
        children = list(trace.children)
        row = next(r for r in trace.query(ToolCallRow) if r.call_id == "r1")
        assert children.index(row) < children.index(reply)


@pytest.mark.asyncio
async def test_grouping_rule_admitted_closes(tmp_path):
    """Впуск реплики из очереди закрывает группу."""
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        await _tail(pilot, thread, *_reads(0))
        scr._top_blocks().queued_admitted()
        await _tail(pilot, thread, *_reads(1))
        assert _block_of(scr, "r0") != _block_of(scr, "r1")


@pytest.mark.asyncio
async def test_grouping_rule_admitted_reply_from_thread_closes(tmp_path):
    """Реплика очереди, сверенная с записью ядра, — впуск: группа
    закрыта, новые чтения — новый блок под репликой."""
    from gc_tui.widgets.user_reply import is_queued

    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        await _tail(pilot, thread, *_reads(0))
        cid = scr._track_pending(display_text="ещё", wire_content="ещё")
        reply = scr._mount_user_reply("ещё", state="queued", client_id=cid)
        await pilot.pause()
        user = {"id": "u2", "role": "user", "content": "ещё"}
        await _tail(pilot, thread, user)
        assert not is_queued(reply)
        await _tail(pilot, thread, *_reads(1))
        assert _block_of(scr, "r0") != _block_of(scr, "r1")
        trace = scr.query_one("#trace", VerticalScroll)
        children = list(trace.children)
        row = next(r for r in trace.query(ToolCallRow) if r.call_id == "r1")
        assert children.index(reply) < children.index(row)


@pytest.mark.asyncio
async def test_grouping_rule_turn_line_closes(tmp_path):
    """Строка итога хода закрывает группу в любом виде; замена строки
    без записи поздней записью блоки не склеивает."""
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        await _tail(pilot, thread, *_reads(0))
        thread.record()
        await _tail(pilot, thread)
        assert scr.query(TurnLine)
        await _tail(pilot, thread, *_reads(1))
        assert _block_of(scr, "r0") != _block_of(scr, "r1")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["held", "fallback"])
async def test_grouping_rule_held_or_fallback_turn_line_closes(tmp_path, kind):
    """Придержанная за баром строка (`-held`, не видна) и строка без
    записи (запасной исход) — тоже события ленты: закрывают."""
    from gc_tui.services.trace_render import build_turn_line

    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        await _tail(pilot, thread, *_reads(0))
        if kind == "held":
            line = build_turn_line(word="cancelled")
            line.add_class("-held")
            scr._trace_mount(line, above_sent=True)
        else:
            scr._show_turn_outcome("completed", False, None)
        await pilot.pause()
        assert scr.query(TurnLine)
        await _tail(pilot, thread, *_reads(1))
        assert _block_of(scr, "r0") != _block_of(scr, "r1")


@pytest.mark.asyncio
async def test_grouping_rule_compaction_marker_closes(tmp_path):
    """Маркер сжатия посреди хода — событие ленты: закрывает."""
    thread = _Thread(tmp_path)
    summary = (
        '<technical type="dialog_summary"><summary>сжато</summary></technical>'
    )
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        await _tail(pilot, thread, *_reads(0))
        await _tail(
            pilot, thread, {"id": "s1", "role": "user", "content": summary}
        )
        assert scr._summary_marker_widgets
        await _tail(pilot, thread, *_reads(1))
        assert _block_of(scr, "r0") != _block_of(scr, "r1")


@pytest.mark.asyncio
async def test_plan_panel_closes(tmp_path):
    """Рамка плана перед живым стримом и над свежей строкой итога —
    через точку закрытия."""
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        await _tail(pilot, thread, *_reads(0))
        stream = Static("пишу…")
        scr._stream_block = stream
        scr._trace_mount(stream)
        await pilot.pause()
        scr._ensure_plan_panel_last({"text": "1. шаг"})
        await pilot.pause()
        stream.remove()
        scr._stream_block = None
        await _tail(pilot, thread, *_reads(1))
        assert _block_of(scr, "r0") != _block_of(scr, "r1")


@pytest.mark.asyncio
async def test_plan_panel_above_fresh_line_closes(tmp_path):
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        calls = []
        scr._note_top_event = lambda widget: calls.append(widget)
        scr._ensure_plan_panel_last({"text": "1. шаг"}, above_fresh_line=True)
        await pilot.pause()
        from gc_tui.widgets.plan_panel import PlanPanel

        assert any(isinstance(w, PlanPanel) for w in calls)


@pytest.mark.asyncio
async def test_stream_does_not_close(tmp_path):
    """Живой стрим группу не закрывает; закрывает событие прозы."""
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        await _tail(pilot, thread, *_reads(0))
        stream = Static("пишу…")
        scr._stream_answer_row = stream
        scr._trace_mount(stream)
        await pilot.pause()
        stream.remove()
        scr._stream_answer_row = None
        await _tail(pilot, thread, *_reads(1))
        assert _block_of(scr, "r0") == _block_of(scr, "r1")
        await _tail(pilot, thread, _answer("a1", "Дальше."))
        await _tail(pilot, thread, *_reads(2))
        assert _block_of(scr, "r2") != _block_of(scr, "r1")


@pytest.mark.asyncio
async def test_nested_fallback_does_not_close_top(tmp_path):
    """Вложенный виджет без якоря в ленте встаёт в конец и группу
    верхнего уровня не закрывает."""
    thread = _Thread(tmp_path)
    agent = {
        "id": "fc_a1",
        "type": "function_call",
        "call_id": "a1",
        "name": "subagent_worker",
        "arguments": json.dumps({"task": "проверь"}),
    }
    done = {
        "id": "fco_a1",
        "type": "function_call_output",
        "call_id": "a1",
        "name": "subagent_worker",
        "output": "{}",
    }
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        await _tail(pilot, thread, agent, done)
        await _tail(pilot, thread, *_reads(0))
        # Якоря (строки `Agent`) в ленте нет — запасной путь.
        trace = scr.query_one("#trace", VerticalScroll)
        next(r for r in trace.query(ToolCallRow) if r.call_id == "a1").remove()
        await pilot.pause()
        nested = Static("вложенное")
        scr._nested_mount_after("a1", nested)
        await pilot.pause()
        assert nested.parent is scr.query_one("#trace")
        await _tail(pilot, thread, *_reads(1))
        assert _block_of(scr, "r0") == _block_of(scr, "r1")


@pytest.mark.asyncio
async def test_issued_widgets_do_not_close(tmp_path):
    """Заголовки, фразы и строки блока — события модели, не ленты."""
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        await _tail(pilot, thread, *_reads(0, 1, 2, 3, 4))
        trace = scr.query_one("#trace", VerticalScroll)
        assert list(trace.query(BlockAction))
        assert len(_blocks(scr)) == 1


def _mount_owners(path) -> dict[str, set[str]]:
    """Функции экрана с `trace.mount(` и что ещё они зовут."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    owners: dict[str, set[str]] = {}
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef):
            continue
        calls = {
            node.func.attr
            for node in ast.walk(func)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
        }
        mounts = [
            node
            for node in ast.walk(func)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "mount"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "trace"
        ]
        if mounts:
            owners[func.name] = calls
    return owners


def test_top_mounts_go_through_closing_point():
    """Каждое `trace.mount(` экрана — через точку закрытия, вложенное,
    виджет блока или бренд пустой ленты."""
    owners = _mount_owners(TUI_PKG / "screens" / "main.py")
    through_point = {"_trace_mount", "_ensure_plan_panel_last"}
    exempt = {
        "_mount_block_widget",  # виджеты модели блоков
        "_nested_mount_after",  # вложенные субагента
        "_show_brand",  # пустая лента, групп ещё нет
    }
    assert set(owners) <= through_point | exempt, sorted(owners)
    for name in through_point:
        assert "_note_top_event" in owners[name], name


# --- прокрутка ----------------------------------------------------


async def _toggle(pilot, scr, key):
    from gc_tui.messages import BlockToggle

    scr.post_message(BlockToggle("", key))
    await pilot.pause()
    await pilot.pause()


@pytest.mark.asyncio
async def test_expand_at_bottom_stays_bottom(tmp_path):
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 20)) as pilot:
        scr = await _open(pilot)
        await _tail(pilot, thread, *_reads(*range(8)))
        trace = scr.query_one("#trace", VerticalScroll)
        await pilot.pause()
        assert trace.scroll_y == trace.max_scroll_y
        await _toggle(pilot, scr, "block:0:top")
        assert all(
            r.display for r in trace.query(ToolCallRow) if r.mode != "header"
        )
        assert trace.scroll_y == trace.max_scroll_y


async def _two_blocks(pilot, thread):
    """Два блока чтений, разделённые ответом; лента у низа."""
    scr = await _open(pilot)
    await _tail(pilot, thread, *_reads(*range(8)))
    await _tail(pilot, thread, _answer("a1", "Посмотрю ещё."))
    await _tail(pilot, thread, *_reads(*range(10, 18)))
    for _ in range(4):
        await pilot.pause()
    return scr


def _header_of(scr, block):
    trace = scr.query_one("#trace", VerticalScroll)
    return next(
        r
        for r in trace.query(ToolCallRow)
        if r.mode == "header" and r.call_id == f"/{block}"
    )


async def _toggle_frames(pilot, monkeypatch, scr, key, header):
    """Кадры после переключения блока: (scroll_y ленты, ряд заголовка)
    на каждой отрисовке экрана."""
    from textual.app import App

    from gc_tui.messages import BlockToggle

    trace = scr.query_one("#trace", VerticalScroll)
    frames: list[tuple[int, int]] = []
    display = App._display

    def recorded(app, screen, renderable):
        frames.append((round(trace.scroll_y), header.region.y))
        return display(app, screen, renderable)

    monkeypatch.setattr(App, "_display", recorded)
    scr.post_message(BlockToggle("", key))
    for _ in range(8):
        await pilot.pause()
    monkeypatch.setattr(App, "_display", display)
    return frames


@pytest.mark.asyncio
@pytest.mark.parametrize("ends", [("top",), ("top", "bottom")])
async def test_toggle_at_bottom_one_movement(tmp_path, monkeypatch, ends):
    """Лента у низа: раскрытие и свёртка последнего блока — одно движение,
    первый кадр после переключения уже итоговый и прижат к низу."""
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 20)) as pilot:
        scr = await _two_blocks(pilot, thread)
        trace = scr.query_one("#trace", VerticalScroll)
        block = _block_of(scr, "r10")
        header = _header_of(scr, block)
        for end in ends:
            frames = await _toggle_frames(
                pilot, monkeypatch, scr, f"{block}:{end}", header
            )
        assert frames
        assert set(frames) == {frames[-1]}
        assert frames[0][0] == trace.max_scroll_y


@pytest.mark.asyncio
@pytest.mark.parametrize("ends", [("top",), ("top", "bottom")])
async def test_toggle_scrolled_up_header_stays(tmp_path, monkeypatch, ends):
    """Лента прокручена вверх: прокрутка не меняется, заголовок блока —
    на своём ряду в каждом кадре."""
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 20)) as pilot:
        scr = await _two_blocks(pilot, thread)
        trace = scr.query_one("#trace", VerticalScroll)
        block = _block_of(scr, "r0")
        header = _header_of(scr, block)
        trace.scroll_to(y=header.virtual_region.y - 6, animate=False)
        for _ in range(3):
            await pilot.pause()
        assert scr._stick_bottom is False
        before = (round(trace.scroll_y), header.region.y)
        for end in ends:
            frames = await _toggle_frames(
                pilot, monkeypatch, scr, f"{block}:{end}", header
            )
        assert set(frames) <= {before}


@pytest.mark.asyncio
async def test_collapse_brings_header_into_view(tmp_path):
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 14)) as pilot:
        scr = await _open(pilot)
        await _tail(pilot, thread, *_reads(*range(12)))
        await _toggle(pilot, scr, "block:0:top")
        trace = scr.query_one("#trace", VerticalScroll)
        trace.scroll_to(y=trace.max_scroll_y // 2, animate=False)
        scr._stick_bottom = False
        await pilot.pause()
        await _toggle(pilot, scr, "block:0:bottom")
        (header,) = [r for r in trace.query(ToolCallRow) if r.mode == "header"]
        top = trace.scroll_offset.y
        assert top <= header.virtual_region.y < top + trace.size.height


# --- стоп и конец хода ------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_closed_block_still_interrupted(tmp_path):
    """Запись стопа закрыла блок строкой «■ Отменено»; поток вышел —
    члены закрытого блока переведены, глиф `○`."""
    from gc_core.persistence.turn_outcome import USER_STOP

    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        await _tail(pilot, thread, *_reads(0), _read(1), _read(2))
        thread.record(status="stopped", reason=USER_STOP)
        await _tail(pilot, thread)
        meta = {
            "thread_id": TID,
            "system_id": "cli",
            "tui": {"status": "stopped"},
        }
        (tmp_path / f"meta_cli_{TID}.json").write_text(json.dumps(meta))
        scr._poll_tick()
        await pilot.pause()
        trace = scr.query_one("#trace", VerticalScroll)
        rows = {r.call_id: r for r in trace.query(ToolCallRow)}
        (header,) = [r for r in rows.values() if r.mode == "header"]
        assert header._render_prefix().plain.strip() == "○"
        bodies = [b.plain for b in rows["r1"]._consequence_bodies()]
        assert bodies == ["отменено"]
        bodies = [b.plain for b in rows["r2"]._consequence_bodies()]
        assert bodies == ["не запущено (стоп)"]


class _ReadsExecutor:
    """Ход: реплика, два чтения, шаг, запись итога — выход. ``answer`` —
    проза между блоком и строкой итога; без неё блок — последний."""

    def __init__(self, threads_dir, *, answer: bool = True):
        self.dir = str(threads_dir)
        self.answer = answer
        self.seq = 0
        self.at = -1
        self.release = threading.Event()

    def _messages(self, tid, *payloads):
        append_new_thread_messages(
            self.dir, "cli", tid, list(payloads), set(), start_seq=self.seq
        )
        self.seq += len(payloads)

    async def arun(self, messages, *, thread_id, message_ids=None, **_kw):
        mid = (message_ids or [None])[0]
        self._messages(
            thread_id, {"id": mid, "role": "user", "content": "первый"}
        )
        self._messages(thread_id, *_reads(0, 1))
        if self.answer:
            self._messages(thread_id, _answer("a1", "готово"))
        self.at = 0
        self.release.wait(10)
        outcome = TurnOutcome(
            status="done",
            reason=None,
            opened_by=mid,
            started_at="2026-10-22T11:30:00Z",
            ended_at="2026-10-22T11:31:12Z",
            duration_sec=12.0,
            error=None,
        )
        turn_outcome_record(self.dir, "cli", thread_id, outcome)

    def stop_run(self):
        self.release.set()

    def turn_effective_start(self):
        return None

    def add_message(self, message):
        pass

    def withdraw_message(self, msg_id):
        return False

    def take_undelivered_messages(self):
        return []


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [True, False], ids=["prose", "block_last"])
async def test_end_of_turn_one_move_with_blocks(tmp_path, answer):
    """Ход с блоком `Explored`: на спадающем фронте поле ввода на месте,
    кадра «бар снят, строки нет» нет (S10) — и когда блок стоит последним
    прямо над строкой итога, без прозы между ними."""
    meta = {"thread_id": TID, "status": "running", "messages_count": 0}
    (tmp_path / f"meta_cli_{TID}.json").write_text(json.dumps(meta))
    (tmp_path / f"data_cli_{TID}.jsonl").write_text("")
    ex = _ReadsExecutor(tmp_path, answer=answer)
    app = _app(tmp_path)
    app._executor_factory = lambda: ex
    frames: list[tuple] = []
    async with app.run_test(size=(100, 30)) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(TID, is_resume=True)
        layout = scr._refresh_layout

        def recorded(*args, **kwargs):
            layout(*args, **kwargs)
            lines = list(scr.query_one("#trace").query(TurnLine))
            frames.append(
                (
                    scr.query_one("#indicator-bar").has_class("-turn"),
                    scr.query_one("#input-row").region.y,
                    any(w.display and w.region.height > 0 for w in lines),
                )
            )

        scr._refresh_layout = recorded
        try:
            pilot.app.query_one("#cmdinput").text = "первый"
            await pilot.press("enter")
            await wait_until(pilot, lambda: ex.at == 0, "шаг")
            await pilot.pause(0.3)
            assert _blocks(scr)
            ex.release.set()
            await wait_until(
                pilot,
                lambda: not pilot.app.active_runner.is_running(),
                "прогон вышел",
            )
            await wait_until(
                pilot, lambda: frames and frames[-1][2], "строка видна"
            )
            await pilot.pause(0.6)
            above = _above_turn_line(scr)
        finally:
            ex.stop_run()
        if not answer:
            assert isinstance(above, ToolCallRow), above
            assert _block_of(scr, above.call_id) == "block:0"
        last_up = max(i for i, f in enumerate(frames) if f[0])
        tail = frames[last_up:]
        assert {y for _up, y, _shown in tail} == {tail[0][1]}
        assert not [f for f in tail if not f[0] and not f[2]]
