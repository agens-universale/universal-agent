"""Превью `/sessions` рисует блоки вызовов тем же исполнителем и журналом
правок (спека P4 §9.7, §9.10а, §15; план Task 18).

Паритет: один тред — живая лента после завершения хода = возврат в
сессию = превью; строка итога хода — только головой (у превью нет
сводки и причины, S10 §4.8). Каждый кадр проходит `assert_trace_grid`.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("textual")

from gc_core.agent_edits.journal import EditRecord, EditsJournal
from gc_core.execution.progress import (
    QueueSubagentProgressSink,
    SubagentProgressEvent,
    run_command_text,
    tool_args_summary,
)
from gc_core.persistence.turn_outcome import USER_STOP, TurnOutcome
from gc_core.thread_store import (
    append_new_thread_messages,
    append_subagent_trace_record,
    append_turn_outcome_record,
    edits_journal_path,
)
from gc_testkit.paths import TUI_PKG
from gc_testkit.trace_grid import assert_trace_grid, trace_rows
from gc_testkit.tui_support import tui_settings
from rich.style import Style

from gc_tui.widgets import tool_call_row
from gc_tui.widgets.tool_blocks_view import HIDDEN_CLASS, BlockAction
from gc_tui.widgets.tool_call_row import ToolCallRow
from gc_tui.widgets.turn_line import TurnLine

TID = "preview00000000000000000000000001"
ORIGIN = 1
SIZE = (100, 120)
AGENT = "s1"


@pytest.fixture(autouse=True)
def steady_rows(monkeypatch):
    monkeypatch.setattr(tool_call_row, "blink_on", lambda frame: True)
    monkeypatch.setattr(
        tool_call_row, "time", SimpleNamespace(monotonic=lambda: 100.0)
    )


# --- тред `p4-21-turn` ------------------------------------------------------


def _call(call_id, name, args):
    return {
        "id": f"fc_{call_id}",
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(args, ensure_ascii=False),
    }


def _out(call_id, name, output=None):
    return {
        "id": f"fco_{call_id}",
        "type": "function_call_output",
        "call_id": call_id,
        "name": name,
        "output": output or json.dumps({"tool": name, "ok": True}),
    }


def _say(mid, text):
    return {"id": mid, "role": "assistant", "content": text}


def _envelope(stdout: str, exit_code: int = 0) -> str:
    return json.dumps(
        {
            "tool": "exec_command",
            "ok": exit_code == 0,
            "command": "x",
            "exit_code": exit_code,
            "timed_out": False,
            "error": None,
            "stdout": {"content": stdout},
            "stderr": {"content": ""},
        },
        ensure_ascii=False,
    )


def _run(call_id, command, stdout, exit_code=0):
    name = "exec_command"
    return [
        _call(call_id, name, {"command": command}),
        _out(call_id, name, _envelope(stdout, exit_code)),
    ]


def _read(i):
    name = "fs_read_file"
    output = json.dumps({"tool": name, "ok": True, "lines": 10 + i})
    return [
        _call(f"r{i}", name, {"path": f"client/m{i}.py"}),
        _out(f"r{i}", name, output),
    ]


def _edit(i):
    name = "fs_apply_file_edits"
    return [_call(f"e{i}", name, {"path": f"e{i}.py"}), _out(f"e{i}", name)]


_SCRIPT = "python - <<'EOF'\nimport json\nprint(1)\nprint(2)\nEOF"
_NESTED_READ = json.dumps({"tool": "fs_read_file", "ok": True, "lines": 7})


def _before_agent() -> list[dict]:
    return [
        _say("a1", "Посмотрю клиент."),
        *[p for i in range(4) for p in _read(i)],
        *_run("x1", "uv run pytest -q", "F\nfailed 1\n", exit_code=1),
        _say("a2", "Поправлю."),
        *[p for i in range(3) for p in _edit(i)],
        *_run("x2", "python gen.py", "ok\n"),
        *_run("x3", _SCRIPT, "1\n"),
        _call(AGENT, "subagent_worker", {"task": "проверка"}),
    ]


def _after_agent() -> list[dict]:
    return [
        _out(AGENT, "subagent_worker", "{}"),
        _say("a3", "Готово."),
    ]


def _calls() -> dict:
    ids = [f"r{i}" for i in range(4)] + [f"e{i}" for i in range(3)]
    ids += ["x1", "x2", "x3", AGENT, "n0"]
    return {cid: 1.0 for cid in ids}


def _hunks(n: int) -> str:
    return f"@@ -0,0 +1,{n} @@\n" + "".join(f"+v{i}\n" for i in range(n))


def _journal(tmp_path) -> None:
    journal = EditsJournal(edits_journal_path(str(tmp_path), "cli", TID))
    for i in range(3):
        journal.append(_record(f"e{i}", f"e{i}.py", n=5 + i))
    journal.append(
        _record(None, "gen.py", n=4, op="create", command_call_id="x2")
    )


def _record(
    call_id, path, *, n, op="edit", command_call_id=None, turn_id="t1"
):
    return EditRecord(
        path=path,
        turn_id=turn_id,
        ts=1.0,
        call_id=call_id,
        op=op,
        hunks=_hunks(n),
        add=n,
        del_=0,
        before_sha256=None,
        after_sha256=None,
        flags=[],
        command_call_id=command_call_id,
    )


def _sidecar(tmp_path) -> None:
    call = {
        "type": "function_call",
        "name": "fs_read_file",
        "arguments": json.dumps({"path": "client/x.py"}),
        "call_id": "n0",
    }
    end = {"type": "function_call_output", "call_id": "n0", "output": ""}
    end["output"] = _NESTED_READ
    for kind, payload in (
        ("call", call),
        ("result", end),
        ("intermediate_reply", {"prose": "Клиент в порядке."}),
    ):
        append_subagent_trace_record(
            str(tmp_path),
            "cli",
            TID,
            parent_call_id=AGENT,
            child_run_id="c",
            kind=kind,
            payload=payload,
        )


def _progress() -> list:
    base = dict(parent_call_id=AGENT, child_run_id="c", step=1, max_steps=9)
    args = json.dumps({"path": "client/x.py"})
    return [
        SubagentProgressEvent(
            kind="tool_start",
            elapsed_ms=0,
            tool_name="fs_read_file",
            tool_call_id="n0",
            args_summary=tool_args_summary("fs_read_file", args),
            command=run_command_text("fs_read_file", args),
            **base,
        ),
        SubagentProgressEvent(
            kind="tool_end",
            elapsed_ms=0,
            tool_name="fs_read_file",
            tool_call_id="n0",
            ok=True,
            output=_NESTED_READ,
            **base,
        ),
        SubagentProgressEvent(
            kind="intermediate_reply",
            elapsed_ms=0,
            prose="Клиент в порядке.",
            **base,
        ),
    ]


class _Thread:
    def __init__(self, tmp_path, *, status="running"):
        self.dir = str(tmp_path)
        self.seq = 0
        meta = {"thread_id": TID, "system_id": "cli", "status": status}
        (tmp_path / f"meta_cli_{TID}.json").write_text(
            json.dumps(meta), encoding="utf-8"
        )
        (tmp_path / f"data_cli_{TID}.jsonl").write_text("", encoding="utf-8")
        self.add({"id": "u1", "role": "user", "content": "почини ретраи"})

    def add(self, *payloads):
        append_new_thread_messages(
            self.dir, "cli", TID, list(payloads), set(), start_seq=self.seq
        )
        self.seq += len(payloads)

    def record(self):
        outcome = TurnOutcome(
            status="done",
            reason=None,
            opened_by="u1",
            started_at="2026-10-22T11:30:00Z",
            ended_at="2026-10-22T11:30:12Z",
            duration_sec=12.0,
            error=None,
            calls=_calls(),
        )
        append_turn_outcome_record(self.dir, "cli", TID, outcome, seq=self.seq)


def _write_done(tmp_path) -> None:
    thread = _Thread(tmp_path, status="stopped")
    thread.add(*_before_agent(), *_after_agent())
    thread.record()
    _journal(tmp_path)
    _sidecar(tmp_path)


# --- экраны --------------------------------------------------------------------


def _app(tmp_path, *, replay: int = 20):
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {
                "agent.model": "M",
                "agent_edits": {
                    "trace_inline_max_lines": 12,
                    "replay_expanded_turns": replay,
                },
            }
        ),
    )
    app._subagent_progress_sink = QueueSubagentProgressSink()
    return app


def _frame(pilot) -> list[tuple[str, str]]:
    """Кадр ленты от реплики: текст строки и цвет её первого знака."""
    rows = trace_rows(pilot.app, exclude=(TurnLine,))
    start = next(i for i, r in enumerate(rows) if r.plain.startswith(" ›"))
    rows = [r for r in rows[start:] if r.plain.strip()]
    assert_trace_grid(rows, origin=ORIGIN)
    return [(r.plain.rstrip(), _lead_colour(r)) for r in rows]


def _lead_colour(row) -> str:
    plain = row.plain
    col = len(plain) - len(plain.lstrip(" "))
    colour = ""
    for span in row.spans:
        if span.start <= col < span.end:
            style = span.style
            if isinstance(style, str):
                style = Style.parse(style)
            if style is not None and style.color is not None:
                colour = style.color.get_truecolor().hex.lower()
    return colour


def _heads(screen) -> list[str]:
    """Головы строк итога хода: без сводки справа и причины под `✕`."""
    return [
        line.text.plain.split("  ")[0]
        for line in screen.query(TurnLine)
        if line.display
    ]


async def _live(tmp_path):
    thread = _Thread(tmp_path)
    _journal(tmp_path)
    async with _app(tmp_path).run_test(size=SIZE) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(TID, is_resume=True)
        await pilot.pause()
        for payload in _before_agent():
            thread.add(payload)
            scr._poll_trace_tail()
            await pilot.pause()
        _sidecar(tmp_path)
        for ev in _progress():
            pilot.app._subagent_progress_sink.emit(ev)
        scr._drain_subagent_progress()
        await pilot.pause()
        thread.add(*_after_agent())
        thread.record()
        scr._poll_trace_tail()
        await pilot.pause()
        return _frame(pilot), _heads(scr)


async def _reopen(tmp_path):
    _write_done(tmp_path)
    async with _app(tmp_path).run_test(size=SIZE) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(TID, is_resume=True)
        await pilot.pause()
        return _frame(pilot), _heads(scr)


async def _open_preview(pilot):
    from gc_tui.screens.preview import PreviewScreen

    scr = pilot.app.screen
    scr.attach_active_task(TID, is_resume=True)
    await pilot.pause()
    store = scr._store
    preview = PreviewScreen(
        segments=store.read_segments(TID),
        thread_id=TID,
        project_path="",
        subagent_records=store.read_subagent_trace(TID),
    )
    await pilot.app.push_screen(preview)
    await pilot.pause()
    return preview


async def _preview(tmp_path):
    _write_done(tmp_path)
    async with _app(tmp_path).run_test(size=SIZE) as pilot:
        preview = await _open_preview(pilot)
        return _frame(pilot), _heads(preview)


def _dirs(tmp_path, *names):
    out = []
    for name in names:
        folder = tmp_path / name
        folder.mkdir()
        out.append(folder)
    return out


# --- паритет ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_equals_reopen_equals_preview(tmp_path):
    """Тред `p4-21-turn` (чтения, упавшая команда, правки, команда с
    файлами, многострочная команда, субагент): тексты и цвет глифов
    живой ленты = возврата = превью; строка итога — головой."""
    live_dir, reopen_dir, preview_dir = _dirs(
        tmp_path, "live", "reopen", "preview"
    )
    live, live_heads = await _live(live_dir)
    reopen, reopen_heads = await _reopen(reopen_dir)
    preview, preview_heads = await _preview(preview_dir)
    assert live == reopen
    assert preview == reopen
    assert live_heads == reopen_heads == preview_heads
    texts = [text for text, _ in preview]
    assert any("Edited 3 файла (+18)" in t for t in texts)
    assert any(t.startswith("     Added gen.py (+4)") for t in texts)
    assert any("Agent  проверка" in t for t in texts)


@pytest.mark.asyncio
async def test_preview_without_journal_edit_has_no_counts(tmp_path):
    """Нет журнала правок — `Edited путь` без счётчиков и диффа (§9.10а)."""
    thread = _Thread(tmp_path, status="stopped")
    thread.add(*_edit(0))
    async with _app(tmp_path).run_test(size=SIZE) as pilot:
        await _open_preview(pilot)
        texts = [text for text, _ in _frame(pilot)]
    assert " ● Edited e0.py" in texts
    assert not any("(+" in t for t in texts)


@pytest.mark.asyncio
async def test_preview_journal_failure_is_logged(
    tmp_path, monkeypatch, caplog
):
    """Сбой чтения журнала правок в превью — правка без счётчиков, причина
    — предупреждением в лог, не молча."""
    import logging

    from gc_tui.services import block_edits

    def broken(*_a, **_kw):
        raise OSError("журнал недоступен")

    monkeypatch.setattr(block_edits, "call_edit", broken)
    thread = _Thread(tmp_path, status="stopped")
    thread.add(*_edit(0))
    caplog.set_level(logging.WARNING, logger="gc_tui.services.trace_render")
    async with _app(tmp_path).run_test(size=SIZE) as pilot:
        await _open_preview(pilot)
        texts = [text for text, _ in _frame(pilot)]
    assert " ● Edited e0.py" in texts
    (record,) = [
        r for r in caplog.records if r.name == "gc_tui.services.trace_render"
    ]
    assert "журнал недоступен" in record.getMessage()


@pytest.mark.asyncio
async def test_preview_enter_opens_thread_and_space_toggles(tmp_path):
    """Enter в превью — открыть тред (`priority`), пробел на заголовке —
    раскрыть блок; одинаковые `block:0` верхнего уровня и субагента не
    путаются."""
    thread = _Thread(tmp_path, status="stopped")
    thread.add(*[p for i in range(5) for p in _read(i)])
    thread.add(_call(AGENT, "subagent_worker", {"task": "проверка"}))
    thread.add(_out(AGENT, "subagent_worker", "{}"))
    for i in range(5):
        append_subagent_trace_record(
            str(tmp_path),
            "cli",
            TID,
            parent_call_id=AGENT,
            child_run_id="c",
            kind="call",
            payload={
                "name": "fs_read_file",
                "arguments": json.dumps({"path": f"n{i}.py"}),
                "call_id": f"n{i}",
            },
        )
        append_subagent_trace_record(
            str(tmp_path),
            "cli",
            TID,
            parent_call_id=AGENT,
            child_run_id="c",
            kind="result",
            payload={"call_id": f"n{i}", "output": _NESTED_READ},
        )
    async with _app(tmp_path).run_test(size=SIZE) as pilot:
        preview = await _open_preview(pilot)
        rows = {r.call_id: r for r in preview.query(ToolCallRow)}
        nested = next(
            r for r in rows.values() if r.block_key == (AGENT, "block:0")
        )
        nested.focus()
        await pilot.pause()
        await pilot.press("space")
        await pilot.pause()
        assert not rows["n0"].has_class(HIDDEN_CLASS)
        assert rows["r0"].has_class(HIDDEN_CLASS)
        phrases = [w for w in preview.query(BlockAction) if w.level == ""]
        assert any(w.phrase == "… ещё 2" for w in phrases)
        dismissed = []
        preview.dismiss = lambda result=None: dismissed.append(result)
        await pilot.press("enter")
        await pilot.pause()
        assert dismissed == [True]


@pytest.mark.asyncio
async def test_preview_old_turn_compact(tmp_path):
    """Ход вне `replay_expanded_turns` — компактный вид и в превью."""
    from gc_tui.widgets.diff_block import DiffBlock

    thread = _Thread(tmp_path, status="stopped")
    thread.add(*_edit(0), _say("a1", "дальше"), *_edit(1))
    journal = EditsJournal(edits_journal_path(str(tmp_path), "cli", TID))
    journal.append(_record("e0", "e0.py", n=5))
    journal.append(_record("e1", "e1.py", n=5, turn_id="t2"))
    async with _app(tmp_path, replay=1).run_test(size=SIZE) as pilot:
        preview = await _open_preview(pilot)
        diffs = list(preview.query(DiffBlock))
        assert [bool(d.query(".block-body")) for d in diffs] == [False, True]
        texts = [text for text, _ in _frame(pilot)]
        assert "     + ещё 5 строк" in texts


@pytest.mark.asyncio
async def test_preview_timers_from_outcome_calls(tmp_path):
    """Таймеры в превью — из `outcome.calls`, включая вложенные."""
    _write_done(tmp_path)
    async with _app(tmp_path).run_test(size=SIZE) as pilot:
        await _open_preview(pilot)
        texts = [text for text, _ in _frame(pilot)]
    assert any("Read  client/x.py · 7 строк · 1с" in t for t in texts)
    assert any("Read  client/m3.py · 13 строк · 1с" in t for t in texts)


def test_trace_render_rows_only_through_model():
    """Страж: в `trace_render.py` нет прямых вызовов методов состояния
    строки вызова — только через модель блоков."""
    import ast

    names = {
        "set_result",
        "mark_interrupted",
        "set_waiting",
        "set_active",
        "pause",
        "resume",
        "set_edit_counts",
    }
    tree = ast.parse(
        (TUI_PKG / "services" / "trace_render.py").read_text(encoding="utf-8")
    )
    calls = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in names
    ]
    assert calls == []


class _FakeRunner:
    """Поток прогона: бежит, пока тест не отпустит (`_running = False`)."""

    _thread_kind = "run"

    def __init__(self):
        self._running = True
        self._last_target = (TID, "cli")
        self.run_seq = 1

    def is_running(self):
        return self._running

    def stop(self):
        pass

    def cancel_ask_human(self):
        pass

    def turn_effective_start(self):
        return None


def _turn_record(thread, opened_by, calls):
    outcome = TurnOutcome(
        status="done",
        reason=None,
        opened_by=opened_by,
        started_at="2026-10-22T11:30:00Z",
        ended_at="2026-10-22T11:31:12Z",
        duration_sec=12.0,
        error=None,
        calls=calls,
    )
    append_turn_outcome_record(thread.dir, "cli", TID, outcome, seq=thread.seq)


async def _ticks(pilot, n=3):
    for _ in range(n):
        pilot.app.screen._poll_tick()
        await pilot.pause()


@pytest.mark.asyncio
async def test_live_equals_reopen_after_undo(tmp_path):
    """Откат отправки до вывода, затем ход с вызовами: живой вид =
    переоткрытию — чистка снятой реплики с курсором не теряет записей
    хода (ee2144256)."""
    from gc_tui.widgets.user_reply import UserReplyContainer

    thread = _Thread(tmp_path, status="stopped")
    thread.add(_say("a0", "привет"))
    _turn_record(thread, "u1", {})
    async with _app(tmp_path).run_test(size=SIZE) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(TID, is_resume=True)
        await pilot.pause()
        pilot.app.tui_state.active_task_id = TID
        old = _FakeRunner()
        pilot.app.active_runner = old
        scr._prepare_for_new_run()
        thread.add({"id": "m_orphan", "role": "user", "content": "отменяю"})
        scr._last_user_message_id = "m_orphan"
        scr._trace_mount(UserReplyContainer("отменяю", event_no=0))
        await pilot.pause()
        scr._cancel_with_undo()
        await pilot.pause()
        new = _FakeRunner()
        pilot.app.active_runner = new
        scr._prepare_for_new_run()
        scr._last_user_message_id = "u2"
        thread.add({"id": "u2", "role": "user", "content": "повтор"})
        old._running = False
        await _ticks(pilot, 2)
        thread.add(*[p for i in range(4) for p in _read(i)])
        thread.add(_say("a2", "Готово."))
        _turn_record(thread, "u2", {f"r{i}": 1.0 for i in range(4)})
        new._running = False
        await _ticks(pilot)
        live = _frame(pilot)
    async with _app(tmp_path).run_test(size=SIZE) as pilot:
        pilot.app.screen.attach_active_task(TID, is_resume=True)
        await pilot.pause()
        reopen = _frame(pilot)
    assert live == reopen
    texts = [text for text, _ in live]
    assert " ● Explored" in texts
    assert "отменяю" not in "".join(texts)


@pytest.mark.asyncio
async def test_preview_stopped_orphan_call_equals_reopen(tmp_path):
    """Стоп, вызов без исхода: превью = переоткрытию — поток стопа давно
    вышел, член `└ отменено`, заголовок `○ Explored` (§2.2, §9.7)."""
    thread = _Thread(tmp_path, status="stopped")
    thread.add(*_read(0), _read(1)[0])
    outcome = TurnOutcome(
        status="stopped",
        reason=USER_STOP,
        opened_by="u1",
        started_at="2026-10-22T11:30:00Z",
        ended_at="2026-10-22T11:30:12Z",
        duration_sec=12.0,
        error=None,
        calls=None,
    )
    append_turn_outcome_record(thread.dir, "cli", TID, outcome, seq=thread.seq)
    async with _app(tmp_path).run_test(size=SIZE) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(TID, is_resume=True)
        await pilot.pause()
        await _ticks(pilot, 1)  # тик опроса: поток не жив — обрыв
        reopen = _frame(pilot)
        await _open_preview(pilot)
        preview = _frame(pilot)
    assert [text for text, _ in reopen] == [
        " › почини ретраи",
        " ○ Explored",
        "   └ Read  client/m0.py · 10 строк",
        "     Read  client/m1.py",
        "       └ отменено",
    ]
    assert preview == reopen
