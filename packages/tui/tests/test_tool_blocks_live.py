"""Живая лента собирает вызовы в блоки (спека P4 §2, §3, §6; план Task 12).

Тред — файлы в `tmp_path`; хвост дописывается, как его пишет ядро, и
дочитывается `_poll_trace_tail`. Каждый кадр ленты проходит сетку колонок
`assert_trace_grid` (строка итога хода сеткой блоков не описывается).
Страж «строки только через модель» — по исходнику экрана.
"""

from __future__ import annotations

import ast
import json

import pytest

pytest.importorskip("textual")

from gc_core.agent_edits.journal import EditRecord, EditsJournal
from gc_core.persistence.turn_outcome import USER_STOP, TurnOutcome
from gc_core.thread_store import (
    append_new_thread_messages,
    append_turn_outcome_record,
    edits_journal_path,
)
from gc_testkit.paths import TUI_PKG
from gc_testkit.trace_grid import assert_trace_grid, trace_rows
from gc_testkit.tui_support import tui_settings
from textual.containers import VerticalScroll

from gc_tui.widgets import tool_call_row
from gc_tui.widgets.diff_block import DiffBlock
from gc_tui.widgets.tool_blocks_view import BlockAction
from gc_tui.widgets.tool_call_row import ToolCallRow
from gc_tui.widgets.turn_line import TurnLine

TID = "blocks00000000000000000000000001"
ORIGIN = 1
SHORT_HUNKS = "@@ -1,2 +1,2 @@\n-a = 1\n+a = 2\n"


def _hunks(n: int) -> str:
    lines = [f"@@ -0,0 +1,{n} @@"] + [f"+line {i}" for i in range(n)]
    return "\n".join(lines) + "\n"


@pytest.fixture(autouse=True)
def steady_blink(monkeypatch):
    """Живой глиф виден в любом кадре — снимок ленты не ловит паузу."""
    monkeypatch.setattr(tool_call_row, "blink_on", lambda frame: True)


# --- тред на диске ----------------------------------------------------------


def _meta(tmp_path, *, status="running", tui=None) -> None:
    meta = {"thread_id": TID, "system_id": "cli", "status": status}
    if tui is not None:
        meta["tui"] = tui
    (tmp_path / f"meta_cli_{TID}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )


class _Thread:
    """Писатель треда, как ядро: сообщения и записи итога хода."""

    def __init__(self, tmp_path, *, status="running"):
        self.dir = str(tmp_path)
        self.seq = 0
        _meta(tmp_path, status=status)
        (tmp_path / f"data_cli_{TID}.jsonl").write_text("", encoding="utf-8")
        # Реплика до открытия: пустой ленты (бренд-блока) нет.
        self.add(_reply())

    def add(self, *payloads):
        append_new_thread_messages(
            self.dir, "cli", TID, list(payloads), set(), start_seq=self.seq
        )
        self.seq += len(payloads)

    def stop(self, opened_by="u1"):
        self.record(opened_by, status="stopped", reason=USER_STOP)

    def record(
        self, opened_by="u1", *, status="done", reason=None, calls=None
    ):
        outcome = TurnOutcome(
            status=status,
            reason=reason,
            opened_by=opened_by,
            started_at="2026-10-22T11:30:00Z",
            ended_at="2026-10-22T11:30:12Z",
            duration_sec=12.0,
            error=None,
            calls=calls,
        )
        append_turn_outcome_record(self.dir, "cli", TID, outcome, seq=self.seq)


def _reply(mid="u1", text="сделай"):
    return {"id": mid, "role": "user", "content": text}


def _call(call_id, name, args):
    return {
        "id": f"fc_{call_id}",
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(args),
    }


def _out(call_id, name, body=None):
    return {
        "id": f"fco_{call_id}",
        "type": "function_call_output",
        "call_id": call_id,
        "name": name,
        "output": json.dumps(body or {"tool": name, "ok": True}),
    }


def _fail(call_id, name, message):
    body = {"tool": name, "ok": False, "error": {"message": message}}
    return _out(call_id, name, body)


def _halt(call_id, name, status):
    return _out(call_id, name, {"tool": name, "ok": False, "status": status})


def _read(i):
    return _call(f"r{i}", "fs_read_file", {"path": f"f{i}.py"})


def _read_out(i):
    return _out(f"r{i}", "fs_read_file")


def _edit_record(call_id, path, *, op="edit", hunks=SHORT_HUNKS, **kw):
    return EditRecord(
        path=path,
        turn_id="t1",
        ts=1.0,
        call_id=call_id,
        op=op,
        hunks=hunks,
        add=kw.get("add", 1),
        del_=kw.get("rem", 1),
        before_sha256=None,
        after_sha256=None,
        flags=kw.get("flags", []),
        size_bytes=kw.get("size"),
    )


def _journal(tmp_path, *records):
    journal = EditsJournal(edits_journal_path(str(tmp_path), "cli", TID))
    for record in records:
        journal.append(record)


# --- экран ------------------------------------------------------------------


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
    scr = pilot.app.screen
    thread.add(*payloads)
    scr._poll_trace_tail()
    await pilot.pause()


def _rows(pilot):
    """Кадр ленты от реплики пользователя (бренд-блок над ней — не
    лента вызовов) — через сетку колонок; непустые строки текстом."""
    rows = trace_rows(pilot.app, exclude=(TurnLine,))
    start = next(i for i, r in enumerate(rows) if r.plain.startswith(" ›"))
    rows = rows[start:]
    assert_trace_grid(rows, origin=ORIGIN)
    return [r.plain[ORIGIN:].rstrip() for r in rows if r.plain.strip()]


def _row(scr, call_id) -> ToolCallRow:
    trace = scr.query_one("#trace", VerticalScroll)
    return next(r for r in trace.query(ToolCallRow) if r.call_id == call_id)


def _headers(scr) -> list[ToolCallRow]:
    trace = scr.query_one("#trace", VerticalScroll)
    return [r for r in trace.query(ToolCallRow) if r.mode == "header"]


def _visible_phrases(scr) -> list[str]:
    trace = scr.query_one("#trace", VerticalScroll)
    return [a.phrase for a in trace.query(BlockAction) if a.display]


# --- Explored / Fetched -----------------------------------------------------


@pytest.mark.asyncio
async def test_exploring_live_then_done(tmp_path):
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        await _open(pilot)
        for i in range(4):
            await _tail(pilot, thread, _read(i), _read_out(i))
        await _tail(pilot, thread, _read(4))
        lines = _rows(pilot)
        assert "● Exploring" in lines
        assert "  └ … ещё 2" in lines
        assert any(line.startswith("    Read  f4.py · ") for line in lines)
        assert not any("f1.py" in line for line in lines)
        await _tail(pilot, thread, _read_out(4))
        lines = _rows(pilot)
        assert "● Explored" in lines
        assert "  └ … ещё 2" in lines
        (header,) = _headers(pilot.app.screen)
        assert header.region.y < _row(pilot.app.screen, "r2").region.y


@pytest.mark.asyncio
async def test_fetched_block(tmp_path):
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        await _open(pilot)
        url = {"url": "https://example.com/a"}
        await _tail(
            pilot,
            thread,
            _call("w1", "exec_fetch_url", url),
            _out("w1", "exec_fetch_url"),
        )
        lines = _rows(pilot)
        assert "● Fetched" in lines
        assert any(line.startswith("  └ Fetch  ") for line in lines)


@pytest.mark.asyncio
async def test_batch_waiting_hidden_live(tmp_path):
    """Батч из 3 чтений: ждущие `•` в группе не видны, со старта — да."""
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        await _tail(pilot, thread, _read(0), _read(1), _read(2))
        lines = _rows(pilot)
        assert not any("•" in line for line in lines)
        assert [_row(scr, f"r{i}").display for i in range(3)] == [
            True,
            False,
            False,
        ]
        await _tail(pilot, thread, _read_out(0))
        assert _row(scr, "r1").display
        assert not _row(scr, "r2").display
        _rows(pilot)


# --- правки -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_edit_diff_column2(tmp_path):
    thread = _Thread(tmp_path)
    _journal(tmp_path, _edit_record("e1", "a.py", add=4, rem=2))
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        edit = {"path": "a.py"}
        await _tail(
            pilot,
            thread,
            _call("e1", "fs_apply_file_edits", edit),
            _out("e1", "fs_apply_file_edits"),
        )
        lines = _rows(pilot)
        assert any(line.startswith("● Edited a.py (+4 −2)") for line in lines)
        diff = scr.query_one(DiffBlock)
        assert diff.region.x - _row(scr, "e1").region.x == 2


@pytest.mark.asyncio
async def test_added_two_same_diffblock(tmp_path):
    """Вторая правка с тем же глаголом — блок; дифф первой тот же
    виджет, теперь в колонке члена."""
    thread = _Thread(tmp_path)
    _journal(
        tmp_path,
        _edit_record("e1", "a.py", op="create", rem=0),
        _edit_record("e2", "b.py", op="create", rem=0),
    )
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        await _tail(
            pilot,
            thread,
            _call("e1", "fs_write_file", {"path": "a.py"}),
            _out("e1", "fs_write_file"),
        )
        first = scr.query_one(DiffBlock)
        await _tail(
            pilot,
            thread,
            _call("e2", "fs_write_file", {"path": "b.py"}),
            _out("e2", "fs_write_file"),
        )
        lines = _rows(pilot)
        assert "● Added 2 файла (+2)" in lines
        assert any(line.startswith("  └ a.py (+1)") for line in lines)
        diffs = list(scr.query(DiffBlock))
        assert diffs[0] is first
        assert first.has_class("diff-member")
        assert first.region.x - scr.query_one("#trace").region.x == 5


@pytest.mark.asyncio
async def test_blank_line_between_files_not_before_first(tmp_path):
    thread = _Thread(tmp_path)
    _journal(
        tmp_path,
        *[_edit_record(f"e{i}", f"f{i}.py") for i in range(3)],
    )
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        for i in range(3):
            await _tail(
                pilot,
                thread,
                _call(f"e{i}", "fs_apply_file_edits", {"path": f"f{i}.py"}),
                _out(f"e{i}", "fs_apply_file_edits"),
            )
        _rows(pilot)
        rows = [_row(scr, f"e{i}") for i in range(3)]
        assert [r.styles.margin.top for r in rows] == [0, 1, 1]
        (header,) = _headers(scr)
        assert rows[0].region.y == header.region.y + 1


@pytest.mark.asyncio
async def test_binary_edit_row(tmp_path):
    thread = _Thread(tmp_path)
    _journal(
        tmp_path,
        _edit_record(
            "e1",
            "img.png",
            op="create",
            hunks="",
            flags=["no_diff"],
            size=12_288,
            add=0,
            rem=0,
        ),
    )
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        await _tail(
            pilot,
            thread,
            _call("e1", "fs_save_base64_file", {"path": "img.png"}),
            _out("e1", "fs_save_base64_file"),
        )
        lines = _rows(pilot)
        assert any(
            line.startswith("● Added img.png · 12 КБ") for line in lines
        )
        assert not scr.query(DiffBlock)


@pytest.mark.asyncio
async def test_failed_edit_rows(tmp_path):
    """Листы `p4-22`: одиночный сбой — `Edit путь` и `└` причины; сбой
    после правки — член блока с причиной на шаг глубже."""
    thread = _Thread(tmp_path)
    _journal(tmp_path, _edit_record("e1", "a.py", add=4, rem=2))
    edit = "fs_apply_file_edits"
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        await _open(pilot)
        await _tail(
            pilot,
            thread,
            _call("e0", edit, {"path": "x.py"}),
            _fail("e0", edit, "не найден фрагмент"),
            _read(0),
            _read_out(0),
        )
        lines = _rows(pilot)
        assert any(line.startswith("● Edit x.py") for line in lines)
        assert "  └ не найден фрагмент" in lines
        await _tail(
            pilot,
            thread,
            _call("e1", edit, {"path": "a.py"}),
            _out("e1", edit),
            _call("e2", edit, {"path": "b.py"}),
            _fail("e2", edit, "не найден фрагмент"),
        )
        lines = _rows(pilot)
        assert "● Edited 2 файла (+4 −2) · 1 ошибка" in lines
        assert any(line.startswith("    b.py") for line in lines)
        assert "      └ не найден фрагмент" in lines


# --- застывшие таймеры -------------------------------------------------


@pytest.mark.asyncio
async def test_frozen_timers_live_and_reopen(tmp_path):
    """Застывшие таймеры вживую и после переоткрытия равны — из
    `outcome.calls`; отказ — без таймера."""
    thread = _Thread(tmp_path)
    refused = {
        "tool": "fs_read_file",
        "ok": False,
        "error": {"type": "action_blocked", "reason": "risky"},
    }
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        await _tail(
            pilot, thread, _read(0), _read_out(0), _read(1), _read_out(1)
        )
        await _tail(
            pilot, thread, _read(2), _out("r2", "fs_read_file", refused)
        )
        thread.record(calls={"r0": 3.0, "r1": 72.0, "r2": 1.0})
        await _tail(pilot, thread)
        _rows(pilot)
        live = _timer_tails(scr)
        assert live["r0"].endswith(" · 3с")
        assert live["r1"].endswith(" · 1м 12с")
        # Отказ таймера не получает, даже если ядро его время записало.
        assert not live["r2"].endswith(" · 1с")
        other = "blocks00000000000000000000000002"
        (tmp_path / f"meta_cli_{other}.json").write_text(
            json.dumps({"thread_id": other, "system_id": "cli"}),
            encoding="utf-8",
        )
        (tmp_path / f"data_cli_{other}.jsonl").write_text("")
        scr.attach_active_task(other, is_resume=True)
        scr.attach_active_task(TID, is_resume=True)
        await pilot.pause()
        assert _timer_tails(scr) == live


def _timer_tails(scr) -> dict[str, str]:
    trace = scr.query_one("#trace", VerticalScroll)
    return {
        r.call_id: r._render_content().plain
        for r in trace.query(ToolCallRow)
        if r.mode != "header"
    }


@pytest.mark.asyncio
async def test_reopen_without_record_no_timers(tmp_path):
    thread = _Thread(tmp_path)
    thread.add(_read(0), _read_out(0))
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        assert not _timer_tails(scr)["r0"].endswith("с")


@pytest.mark.asyncio
async def test_copy_through_collapsed_block_skips_hidden_rows(tmp_path):
    """Выделение мышью через свёрнутый блок (§9.8): скрытые окном члены
    не выделяются и в буфер не попадают, видимые — попадают."""
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        for i in range(5):
            await _tail(pilot, thread, _read(i), _read_out(i))
        (header,) = _headers(scr)
        last = _row(scr, "r4")
        await pilot.mouse_down(header, offset=(0, 0))
        await pilot.hover(last, offset=(30, 0))
        await pilot.mouse_up(last, offset=(30, 0))
        await pilot.pause()
        selected = scr.get_selected_text() or ""
        assert "f2.py" in selected and "f4.py" in selected
        assert "f0.py" not in selected
        assert "f1.py" not in selected


# --- стоп и переоткрытие ----------------------------------------------------


class _SlowRunner:
    """Стоп кооперативный: поток жив, пока тест не доведёт ход до конца."""

    _thread_kind = "run"

    def __init__(self):
        self._last_target = (TID, "cli")
        self._stop_requested = False
        self.running = True
        self.run_seq = 1

    def is_running(self):
        return self.running

    def stop(self):
        self._stop_requested = True

    def cancel_confirm(self):
        pass

    def cancel_ask_human(self):
        pass

    def withdraw_message(self, msg_id):
        return False

    def follow(self, _other):
        pass

    def turn_effective_start(self):
        return None


@pytest.mark.asyncio
async def test_stop_mid_exploring(tmp_path):
    """Запись стопа дочитана, пока поток жив, — строка «■ Отменено»
    придержана за баром, члены ещё без исхода; поток вышел — в одном тике
    члены переведены (идущий `└ отменено`, ждущий `└ не запущено
    (стоп)`) и строка раскрыта под блоком (§2.2)."""
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        runner = _SlowRunner()
        pilot.app.active_runner = runner
        scr._prepare_for_new_run()
        scr._reset_thinking_timer()
        scr._last_user_message_id = None  # без отката: ход что-то выдал
        scr._poll_active()
        await pilot.pause()
        await _tail(pilot, thread, _read(0), _read_out(0))
        await _tail(pilot, thread, _read(1), _read(2))
        scr._cmd_stop()
        await pilot.pause()
        thread.stop()
        scr._poll_active()  # запись стопа дочитана, поток ещё жив
        (line,) = scr.query(TurnLine)
        assert "Отменено" in line.text.plain
        assert line.has_class("-held")
        assert _row(scr, "r1")._block_consequence is None
        runner.running = False
        _meta(tmp_path, tui={"status": "stopped"})
        scr._poll_active()  # поток вышел: один тик
        assert line.display and not line.has_class("-held")
        assert _row(scr, "r1")._block_consequence.plain == "отменено"
        assert _row(scr, "r2")._block_consequence.plain == "не запущено (стоп)"
        await pilot.pause()
        lines = _rows(pilot)
        assert "○ Explored" in lines
        assert "      └ отменено" in lines
        assert "      └ не запущено (стоп)" in lines
        assert not any("прервано" in line for line in lines)
        assert line.region.y > _row(scr, "r2").region.y


@pytest.mark.asyncio
async def test_stop_results_from_core(tmp_path):
    """Стоп с исходами ядра (отменено / не запущено) и записью стопа —
    тот же вид: `○ Explored` и `└` причин цветом стопа."""
    thread = _Thread(tmp_path)
    name = "fs_read_file"
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        await _open(pilot)
        await _tail(pilot, thread, _read(0), _read_out(0))
        await _tail(
            pilot,
            thread,
            _read(1),
            _read(2),
            _halt("r1", name, "cancelled_by_user"),
            _halt("r2", name, "not_started_due_to_user_stop"),
        )
        thread.stop()
        await _tail(pilot, thread)
        lines = _rows(pilot)
        assert "○ Explored" in lines
        assert "      └ отменено" in lines
        assert "      └ не запущено (стоп)" in lines


@pytest.mark.asyncio
async def test_limit_halted_member_keeps_glyph(tmp_path):
    """Член, остановленный лимитом, — `└` серым, но заголовку `○` не
    ставит: `○` только при остановке хода."""
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        await _open(pilot)
        await _tail(
            pilot,
            thread,
            _read(0),
            _read_out(0),
            _read(1),
            _halt("r1", "fs_read_file", "tool_call_limit"),
        )
        lines = _rows(pilot)
        assert "● Explored" in lines
        assert "      └ лимит вызовов" in lines


@pytest.mark.asyncio
async def test_reopen_member_without_result(tmp_path):
    """Тред оборвался посреди вызова: после переоткрытия член без `└` и
    без таймера, у заголовка `○`."""
    thread = _Thread(tmp_path, status="stopped")
    thread.add(_read(0), _read_out(0), _read(1))
    _meta(tmp_path, status="stopped", tui={"status": "stopped"})
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        scr._poll_tick()
        await pilot.pause()
        lines = _rows(pilot)
        assert "○ Explored" in lines
        assert "    Read  f1.py" in lines
        assert not any("└ отменено" in line for line in lines)
        assert not any("прервано" in line for line in lines)


@pytest.mark.asyncio
async def test_child_question_pauses_agent_via_model(tmp_path, monkeypatch):
    """Вопрос ребёнка ставит паузу строке `Agent` через исполнитель
    блоков, ответ снимает."""
    from gc_tui.widgets.tool_blocks_view import ToolBlocksView

    seen: list[tuple[str, str]] = []
    paused, resumed = ToolBlocksView.paused, ToolBlocksView.resumed

    def _paused(self, row):
        seen.append(("paused", row.call_id))
        paused(self, row)

    def _resumed(self, row):
        seen.append(("resumed", row.call_id))
        resumed(self, row)

    monkeypatch.setattr(ToolBlocksView, "paused", _paused)
    monkeypatch.setattr(ToolBlocksView, "resumed", _resumed)
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        await _tail(
            pilot,
            thread,
            _call("a1", "subagent_worker", {"task": "проверь"}),
        )
        scr._show_child_ask_human(
            parent_call_id="a1", prose="какой файл?", via="worker"
        )
        await pilot.pause()
        assert _row(scr, "a1")._paused_at is not None
        scr._hide_child_ask_human(parent_call_id="a1")
        await pilot.pause()
        assert _row(scr, "a1")._paused_at is None
        assert ("paused", "a1") in seen
        assert ("resumed", "a1") in seen


# --- страж ------------------------------------------------------------------

#: Смены состояния строки вызова — только через исполнитель блоков.
_ROW_STATE_CALLS = frozenset(
    {
        "set_result",
        "mark_interrupted",
        "set_waiting",
        "set_active",
        "pause",
        "resume",
        "set_edit_counts",
    }
)


def _row_state_calls(path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    owners: set[str] = set()
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(func):
            if _row_state_call(node):
                owners.add(func.name)
    return owners


def _row_state_call(node) -> bool:
    """Вызов метода состояния строки — и прямой, и по имени-строке
    (`getattr(w, "mark_interrupted")`): обход стража тоже вызов."""
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Attribute):
        return node.func.attr in _ROW_STATE_CALLS
    return (
        isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and any(
            isinstance(arg, ast.Constant) and arg.value in _ROW_STATE_CALLS
            for arg in node.args[1:2]
        )
    )


def test_rows_only_through_model():
    owners = _row_state_calls(TUI_PKG / "screens" / "main.py")
    assert owners == set(), sorted(owners)
