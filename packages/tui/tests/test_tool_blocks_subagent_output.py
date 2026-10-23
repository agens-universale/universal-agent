"""Вывод вложенных вызовов субагента (спека P4 §7, §9.14 п. 5, 7; план
Task 16).

Вживую вывод и команда едут в событии прогресса; сверх потолка поле
пустое — тогда из sidecar (записан до события). После возврата — из
sidecar. Оба пути рисуют одно; каждый кадр — через `assert_trace_grid`.
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
    tool_args_summary,
)
from gc_core.persistence.turn_outcome import TurnOutcome
from gc_core.thread_store import (
    append_new_thread_messages,
    append_subagent_trace_record,
    append_turn_outcome_record,
    edits_journal_path,
)
from gc_testkit.trace_grid import assert_trace_grid, trace_rows
from gc_testkit.tui_support import tui_settings

from gc_tui.widgets import tool_call_row
from gc_tui.widgets.turn_line import TurnLine

TID = "agentout0000000000000000000000001"
ORIGIN = 1
AGENT = "sub1"
HUNKS = "@@ -0,0 +1,2 @@\n+a = 1\n+b = 2\n"


@pytest.fixture(autouse=True)
def steady_rows(monkeypatch):
    monkeypatch.setattr(tool_call_row, "blink_on", lambda frame: True)
    monkeypatch.setattr(
        tool_call_row, "time", SimpleNamespace(monotonic=lambda: 100.0)
    )


def _channel(text: str) -> dict:
    return {"content": text}


def _envelope(stdout: str, *, exit_code: int = 0) -> str:
    return json.dumps(
        {
            "tool": "exec_command",
            "ok": exit_code == 0,
            "command": "x",
            "exit_code": exit_code,
            "timed_out": False,
            "error": None
            if exit_code == 0
            else {
                "type": "non_zero_exit",
                "message": f"Command exited with status {exit_code}",
            },
            "stdout": _channel(stdout),
            "stderr": _channel(""),
        },
        ensure_ascii=False,
    )


def _lines(n: int, word: str = "line") -> str:
    return "".join(f"{word} {i}\n" for i in range(n))


# --- сценарий: один вложенный вызов --------------------------------------


class _Call:
    """Вложенный вызов: имя, аргументы, сырой вывод."""

    def __init__(self, call_id, name, args, output, *, ok=True):
        self.call_id = call_id
        self.name = name
        self.args = args
        self.output = output
        self.ok = ok

    @property
    def raw_args(self) -> str:
        return json.dumps(self.args, ensure_ascii=False)

    def progress(self, *, command_cap=False, output_cap=False) -> list:
        from gc_core.execution.progress import run_command_text

        base = dict(
            parent_call_id=AGENT,
            child_run_id="c",
            step=1,
            max_steps=9,
            elapsed_ms=0,
            tool_name=self.name,
            tool_call_id=self.call_id,
        )
        command = run_command_text(self.name, self.raw_args)
        return [
            SubagentProgressEvent(
                kind="tool_start",
                args_summary=tool_args_summary(self.name, self.raw_args),
                command=None if command_cap else command,
                **base,
            ),
            SubagentProgressEvent(
                kind="tool_end",
                ok=self.ok,
                output=None if output_cap else self.output,
                **base,
            ),
        ]

    def sidecar(self, tmp_path) -> None:
        call = {
            "type": "function_call",
            "name": self.name,
            "arguments": self.raw_args,
            "call_id": self.call_id,
        }
        end = {
            "type": "function_call_output",
            "call_id": self.call_id,
            "output": self.output,
        }
        for kind, payload in (
            ("call", call),
            ("result" if self.ok else "error", end),
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


def _ran(call_id="x1", command="make test", output=None, **kw) -> _Call:
    return _Call(
        call_id,
        "exec_command",
        {"command": command},
        output if output is not None else _envelope(_lines(5)),
        **kw,
    )


# --- тред родителя --------------------------------------------------------


class _Thread:
    def __init__(self, tmp_path, *, status="running"):
        self.dir = str(tmp_path)
        self.seq = 0
        meta = {"thread_id": TID, "system_id": "cli", "status": status}
        (tmp_path / f"meta_cli_{TID}.json").write_text(
            json.dumps(meta), encoding="utf-8"
        )
        (tmp_path / f"data_cli_{TID}.jsonl").write_text("", encoding="utf-8")
        self.add({"id": "u1", "role": "user", "content": "проверь"})

    def add(self, *payloads):
        append_new_thread_messages(
            self.dir, "cli", TID, list(payloads), set(), start_seq=self.seq
        )
        self.seq += len(payloads)

    def record(self, calls):
        outcome = TurnOutcome(
            status="done",
            reason=None,
            opened_by="u1",
            started_at="2026-10-22T11:30:00Z",
            ended_at="2026-10-22T11:30:12Z",
            duration_sec=12.0,
            error=None,
            calls=calls,
        )
        append_turn_outcome_record(self.dir, "cli", TID, outcome, seq=self.seq)


_AGENT_CALL = {
    "id": "fc_sub1",
    "type": "function_call",
    "call_id": AGENT,
    "name": "subagent_worker",
    "arguments": json.dumps({"task": "проверка"}),
}
_AGENT_DONE = {
    "id": "fco_sub1",
    "type": "function_call_output",
    "call_id": AGENT,
    "name": "subagent_worker",
    "output": "{}",
}


def _app(tmp_path):
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {
                "agent.model": "M",
                "agent_edits": {"trace_inline_max_lines": 12},
            }
        ),
    )
    app._subagent_progress_sink = QueueSubagentProgressSink()
    return app


def _rows(pilot) -> list[str]:
    rows = trace_rows(pilot.app, exclude=(TurnLine,))
    start = next(i for i, r in enumerate(rows) if r.plain.startswith(" ›"))
    rows = rows[start:]
    assert_trace_grid(rows, origin=ORIGIN)
    return [r.plain[ORIGIN:].rstrip() for r in rows if r.plain.strip()]


def _agent_rows(lines: list[str]) -> list[str]:
    at = next(i for i, line in enumerate(lines) if "Agent  " in line)
    block = []
    for line in lines[at + 1 :]:
        if not line.startswith("  "):
            break
        block.append(line)
    return block


async def _live(tmp_path, call: _Call, **caps) -> list[str]:
    """Вживую: sidecar пишется до события (как ядро), события — в ленту."""
    thread = _Thread(tmp_path)
    call.sidecar(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(TID, is_resume=True)
        await pilot.pause()
        # Sidecar уже на диске, но вложенных под идущим `Agent` ещё нет.
        thread.add(_AGENT_CALL)
        scr._poll_trace_tail()
        await pilot.pause()
        for ev in call.progress(**caps):
            pilot.app._subagent_progress_sink.emit(ev)
        scr._drain_subagent_progress()
        await pilot.pause()
        return _agent_rows(_rows(pilot))


async def _reopen(tmp_path, call: _Call) -> list[str]:
    thread = _Thread(tmp_path, status="stopped")
    thread.add(_AGENT_CALL, _AGENT_DONE)
    thread.record({AGENT: 0.0, call.call_id: 0.0})
    call.sidecar(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        pilot.app.screen.attach_active_task(TID, is_resume=True)
        await pilot.pause()
        return _agent_rows(_rows(pilot))


def _dirs(tmp_path):
    live, reopen = tmp_path / "live", tmp_path / "reopen"
    live.mkdir()
    reopen.mkdir()
    return live, reopen


# --- вывод -------------------------------------------------------------------


_RAN_5 = [
    "  ● Ran  make test",
    "    └ line 0",
    "      line 1",
    "      line 2",
    "      + ещё 2 строки",
]


@pytest.mark.asyncio
async def test_nested_ran_preview_from_event(tmp_path):
    """Короткий вывод — превью прямо из события `tool_end`."""
    live_dir, reopen_dir = _dirs(tmp_path)
    assert await _live(live_dir, _ran()) == _RAN_5
    assert await _reopen(reopen_dir, _ran()) == _RAN_5


@pytest.mark.asyncio
async def test_nested_ran_output_over_cap_from_sidecar(tmp_path):
    """Вывод сверх потолка (`output=None`) — превью из записи sidecar,
    `+ ещё N строк` то же, что после возврата."""
    live_dir, reopen_dir = _dirs(tmp_path)
    live = await _live(live_dir, _ran(), output_cap=True)
    assert live == await _reopen(reopen_dir, _ran())
    assert live == _RAN_5


@pytest.mark.asyncio
async def test_nested_ran_failed_exit_code(tmp_path):
    call = _ran(output=_envelope(_lines(1, "err"), exit_code=2), ok=False)
    live_dir, reopen_dir = _dirs(tmp_path)
    live = await _live(live_dir, call)
    assert live == await _reopen(reopen_dir, call)
    assert live == [
        "  ● Ran  make test",
        "    └ код выхода 2",
        "      err 0",
    ]


@pytest.mark.asyncio
async def test_nested_read_summary_live_and_reopen(tmp_path):
    """Успешный вложенный `Read` — сводка ` · 40 строк` и вживую, и после
    возврата (маппер вывода верхнего уровня)."""
    output = json.dumps({"tool": "fs_read_file", "ok": True, "lines": 40})
    call = _Call("r1", "fs_read_file", {"path": "a.py"}, output)
    live_dir, reopen_dir = _dirs(tmp_path)
    live = await _live(live_dir, call)
    assert live == await _reopen(reopen_dir, call)
    assert live == ["  ● Explored", "    └ Read  a.py · 40 строк"]


@pytest.mark.asyncio
async def test_nested_ran_with_edits(tmp_path):
    """Правки вложенной команды — по `command_call_id` вложенного вызова
    тем же билдером, что наверху: `Added путь` под выводом."""
    live_dir, reopen_dir = _dirs(tmp_path)
    for folder in (live_dir, reopen_dir):
        journal = EditsJournal(edits_journal_path(str(folder), "cli", TID))
        journal.append(
            EditRecord(
                path="gen.py",
                turn_id="t1",
                ts=1.0,
                call_id=None,
                op="create",
                hunks=HUNKS,
                add=2,
                del_=0,
                before_sha256=None,
                after_sha256=None,
                flags=[],
                command_call_id="x1",
            )
        )
    call = _ran(output=_envelope("ok\n"))
    live = await _live(live_dir, call)
    assert live == await _reopen(reopen_dir, call)
    assert live[:2] == ["  ● Ran  make test", "    └ ok"]
    # Строка файла кончается кнопкой копирования диффа.
    assert live[2].startswith("      Added gen.py (+2 −0) ")


# --- многострочная команда ----------------------------------------------------


_SCRIPT = "python - <<'EOF'\nimport json\nprint(1)\nprint(2)\nEOF"


@pytest.mark.asyncio
async def test_nested_multiline_command_live(tmp_path):
    """Строки команды вложенного `Ran` — из события `tool_start`."""
    call = _ran(command=_SCRIPT, output=_envelope("1\n"))
    live_dir, reopen_dir = _dirs(tmp_path)
    live = await _live(live_dir, call)
    assert live == await _reopen(reopen_dir, call)
    assert live == [
        "  ● Ran  python - <<'EOF'",
        "      import json",
        "      print(1)",
        "    └ 1",
        "      + ещё 2 строки",
    ]


@pytest.mark.asyncio
async def test_nested_multiline_command_over_cap(tmp_path):
    """Команда сверх потолка (`command=None`) — строки из записи sidecar
    `call`, те же, что после возврата."""
    call = _ran(command=_SCRIPT, output=_envelope("1\n"))
    live_dir, reopen_dir = _dirs(tmp_path)
    live = await _live(live_dir, call, command_cap=True)
    assert live == await _reopen(reopen_dir, call)
    assert live[1:3] == ["      import json", "      print(1)"]
