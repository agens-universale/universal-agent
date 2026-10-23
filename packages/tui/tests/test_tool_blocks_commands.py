"""Блок `Ran` (спека P4 §4; план Task 14): превью вывода, код выхода,
многострочная команда, правки под командой — по тесту на макет
`p4-06`…`p4-11`. Каждый кадр — через сетку колонок `assert_trace_grid`.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("textual")

from gc_core.agent_edits.journal import EditRecord, EditsJournal
from gc_core.thread_store import append_new_thread_messages, edits_journal_path
from gc_testkit.trace_grid import assert_trace_grid, trace_rows
from gc_testkit.tui_support import tui_settings
from rich.style import Style
from textual.containers import VerticalScroll

from gc_tui.palette import DIM, ERR, INK, REFUSED, SUB_INK
from gc_tui.widgets import tool_call_row
from gc_tui.widgets.diff_block import DiffBlock
from gc_tui.widgets.tool_blocks_view import BlockToggle
from gc_tui.widgets.tool_call_row import ToolCallRow
from gc_tui.widgets.turn_line import TurnLine

TID = "ran00000000000000000000000000001"
ORIGIN = 1
HUNKS = "@@ -0,0 +1,{n} @@\n" + "{body}"


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
        self.add({"id": "u1", "role": "user", "content": "прогони"})

    def add(self, *payloads):
        append_new_thread_messages(
            self.dir, "cli", TID, list(payloads), set(), start_seq=self.seq
        )
        self.seq += len(payloads)


def _run(call_id, command, name="exec_command"):
    key = "code" if name == "exec_python" else "command"
    return {
        "id": f"fc_{call_id}",
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps({key: command}),
    }


def _channel(text):
    return {"content": text, "total_chars": len(text)}


def _envelope(stdout="", stderr="", **head):
    exit_code = head.pop("exit_code", 0)
    body = {
        "tool": "exec_command",
        "ok": exit_code == 0 and not head.get("timed_out"),
        "command": "x",
        "exit_code": exit_code,
        "timed_out": False,
        "error": None,
        **head,
        "stdout": stdout if isinstance(stdout, dict) else _channel(stdout),
        "stderr": _channel(stderr),
    }
    return json.dumps(body, ensure_ascii=False)


def _out(call_id, output, name="exec_command"):
    return {
        "id": f"fco_{call_id}",
        "type": "function_call_output",
        "call_id": call_id,
        "name": name,
        "output": output,
    }


def _lines(n, word="line"):
    return "".join(f"{word} {i}\n" for i in range(n))


def _shell_record(path, op="shell", *, n=4, paths=None, **kw):
    body = "".join(f"+row {i}\n" for i in range(n))
    return EditRecord(
        path=path,
        turn_id="t1",
        ts=1.0,
        call_id=None,
        op=op,
        hunks=kw.get("hunks", HUNKS.format(n=n, body=body)),
        add=n,
        del_=0,
        before_sha256=None,
        after_sha256=None,
        flags=kw.get("flags", []),
        command_call_id="x1",
        command_paths=paths,
    )


def _journal(tmp_path, *records):
    journal = EditsJournal(edits_journal_path(str(tmp_path), "cli", TID))
    for record in records:
        journal.append(record)


def _app(tmp_path):
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        # Превью диффа одиночной правки — 12 рядов (спека §6.3), не
        # дефолт конфига.
        settings=tui_settings(
            {"agent.model": "M", "agent_edits": {"trace_inline_max_lines": 12}}
        ),
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


def _frame(pilot):
    """Кадр ленты от реплики пользователя — через сетку колонок."""
    rows = trace_rows(pilot.app, exclude=(TurnLine,))
    start = next(i for i, r in enumerate(rows) if r.plain.startswith(" ›"))
    rows = rows[start:]
    assert_trace_grid(rows, origin=ORIGIN)
    return rows


def _rows(pilot) -> list[str]:
    return [
        r.plain[ORIGIN:].rstrip() for r in _frame(pilot) if r.plain.strip()
    ]


def _colour(row, needle) -> str:
    """Цвет текста в начале ``needle`` строки кадра ``row``."""
    col = row.plain.index(needle)
    colour = ""
    for span in row.spans:
        if span.start <= col < span.end:
            style = span.style
            if isinstance(style, str):
                style = Style.parse(style)
            if style is not None and style.color is not None:
                colour = style.color.get_truecolor().hex.lower()
    return colour


def _frame_row(pilot, needle):
    return next(r for r in _frame(pilot) if needle in r.plain)


async def _toggle(pilot, key="block:0"):
    pilot.app.screen.post_message(BlockToggle("", key))
    await pilot.pause()
    await pilot.pause()


# --- p4-06…p4-08: Running, Ran, без вывода ----------------------------------


@pytest.mark.asyncio
async def test_running_no_output(tmp_path):
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        await _open(pilot)
        await _tail(pilot, thread, _run("x1", "uv run pytest -q"))
        lines = _rows(pilot)
        assert any(
            line.startswith("● Running  uv run pytest -q · ") for line in lines
        )
        assert not any("└" in line for line in lines)
        row = _frame_row(pilot, "uv run pytest")
        assert _colour(row, "uv run pytest") == INK.lower()


@pytest.mark.asyncio
async def test_ran_preview_and_more(tmp_path):
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 60)) as pilot:
        await _open(pilot)
        await _tail(
            pilot,
            thread,
            _run("x1", "pytest"),
            _out("x1", _envelope(_lines(14))),
        )
        lines = _rows(pilot)
        assert lines[1].startswith("● Ran  pytest")
        assert lines[2:6] == [
            "  └ line 0",
            "    line 1",
            "    line 2",
            "    + ещё 11 строк",
        ]
        row = _frame_row(pilot, "pytest")
        assert _colour(row, "pytest") == SUB_INK.lower()
        assert _colour(_frame_row(pilot, "line 1"), "line 1") == DIM.lower()
        await _toggle(pilot)
        lines = _rows(pilot)
        assert "    line 13" in lines
        assert lines[-1] == "    − свернуть"


@pytest.mark.asyncio
async def test_ran_empty(tmp_path):
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        await _open(pilot)
        await _tail(
            pilot,
            thread,
            _run("x1", "git add a.py"),
            _out("x1", _envelope()),
        )
        lines = _rows(pilot)
        assert lines[2] == "  └ (нет вывода)"
        assert not any("ещё" in line for line in lines)


# --- p4-09: сбой ------------------------------------------------------------


@pytest.mark.asyncio
async def test_ran_failed_exit_code(tmp_path):
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        await _open(pilot)
        out = _envelope("", _lines(4, "err"), exit_code=2)
        await _tail(pilot, thread, _run("x1", "make docs"), _out("x1", out))
        lines = _rows(pilot)
        assert lines[1].startswith("● Ran  make docs")
        assert _colour(_frame_row(pilot, "Ran  make"), "●") == ERR.lower()
        assert lines[2:7] == [
            "  └ код выхода 2",
            "    err 0",
            "    err 1",
            "    err 2",
            "    + ещё 1 строка",
        ]
        assert _colour(_frame_row(pilot, "код выхода"), "код") == ERR.lower()
        assert not any("· код выхода" in line for line in lines)


@pytest.mark.asyncio
async def test_ran_timeout(tmp_path):
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        await _open(pilot)
        out = _envelope("partial\n", timed_out=True, exit_code=-1)
        await _tail(pilot, thread, _run("x1", "sleep 99"), _out("x1", out))
        lines = _rows(pilot)
        assert lines[2:4] == ["  └ превышено время ожидания", "    partial"]
        row = _frame_row(pilot, "превышено")
        assert _colour(row, "превышено") == ERR.lower()


@pytest.mark.asyncio
async def test_ran_background(tmp_path):
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        await _open(pilot)
        out = _envelope(background={"pid": 7, "output": "not captured"})
        await _tail(pilot, thread, _run("x1", "serve &"), _out("x1", out))
        lines = _rows(pilot)
        assert lines[2:] == ["  └ запущено в фоне · вывод не сохраняется"]


@pytest.mark.asyncio
async def test_ran_omitted_marker(tmp_path):
    thread = _Thread(tmp_path)
    channel = {
        "truncated": True,
        "head": "h0\nh1\nh2\nh3\n",
        "tail": "t0\nt1\n",
        "total_chars": 5000,
        "omitted_chars": 1200,
    }
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        await _open(pilot)
        await _tail(
            pilot,
            thread,
            _run("x1", "build"),
            _out("x1", _envelope(channel)),
        )
        lines = _rows(pilot)
        assert lines[-1] == "    + ещё 3 строки"
        assert not any("пропущено" in line for line in lines)
        await _toggle(pilot)
        lines = _rows(pilot)
        start = lines.index("  └ h0")
        assert lines[start : start + 7] == [
            "  └ h0",
            "    h1",
            "    h2",
            "    h3",
            "    … пропущено 1 200 символов",
            "    t0",
            "    t1",
        ]


@pytest.mark.asyncio
async def test_ran_refused(tmp_path):
    thread = _Thread(tmp_path)
    refused = json.dumps(
        {
            "tool": "exec_command",
            "ok": False,
            "error": {"type": "action_blocked", "reason": "risky"},
        }
    )
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        await _open(pilot)
        await _tail(pilot, thread, _run("x1", "rm -rf /"), _out("x1", refused))
        lines = _rows(pilot)
        assert lines[1].startswith("○ Ran  rm -rf /")
        assert _colour(_frame_row(pilot, "Ran  rm"), "○") == REFUSED.lower()
        assert lines[2:] == ["  └ отклонил судья auto: risky"]
        row = _frame_row(pilot, "отклонил")
        assert _colour(row, "отклонил") == REFUSED.lower()


# --- p4-10: правки под командой ---------------------------------------------


@pytest.mark.asyncio
async def test_ran_files_one_phrase(tmp_path):
    thread = _Thread(tmp_path)
    _journal(
        tmp_path,
        _shell_record("a.py", n=2),
        _shell_record("new.py", "create", n=10),
        _shell_record("b.py", n=2),
        _shell_record("gone.py", "delete", hunks="", flags=["no_diff"]),
    )
    async with _app(tmp_path).run_test(size=(100, 60)) as pilot:
        scr = await _open(pilot)
        await _tail(
            pilot,
            thread,
            _run("x1", "python gen.py"),
            _out("x1", _envelope(_lines(10))),
        )
        lines = _rows(pilot)
        assert lines[2:5] == ["  └ line 0", "    line 1", "    line 2"]
        assert lines[5] == "    … ещё 1"
        assert any(line.startswith("    Added new.py (+10)") for line in lines)
        assert "    Deleted gone.py" in lines
        # Одна фраза — в самом низу: 7 строк вывода, 6 скрытых строк
        # диффа new.py (доля 4 из 10) и 2 строки скрытого a.py.
        assert lines[-1] == "    + ещё 15 строк"
        assert sum("+ ещё" in line for line in lines) == 1
        trace = scr.query_one("#trace", VerticalScroll)
        files = [
            r
            for r in trace.query(ToolCallRow)
            if r.call_id.startswith("x1:") and r.display
        ]
        assert [r.styles.margin.top for r in files] == [0, 1, 1]
        diffs = [d for d in trace.query(DiffBlock) if d.display]
        assert len(diffs) == 2


@pytest.mark.asyncio
async def test_ran_files_unrecorded(tmp_path):
    thread = _Thread(tmp_path)
    _journal(tmp_path, _shell_record("a.py", n=1, paths=4))
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        await _open(pilot)
        await _tail(
            pilot,
            thread,
            _run("x1", "codegen"),
            _out("x1", _envelope("ok\n")),
        )
        lines = _rows(pilot)
        assert "    … ещё 3" in lines
        await _toggle(pilot)
        lines = _rows(pilot)
        assert "    ещё 3 файла не записано" in lines


# --- многострочная команда --------------------------------------------------

_SCRIPT = "python - <<'EOF'\nimport json\nprint(1)\nx = 2\nEOF"


@pytest.mark.asyncio
async def test_ran_multiline_command(tmp_path):
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        await _open(pilot)
        await _tail(
            pilot,
            thread,
            _run("x1", _SCRIPT),
            _out("x1", _envelope("1\n")),
        )
        lines = _rows(pilot)
        assert lines[1].startswith("● Ran  python - <<'EOF'")
        assert lines[2:6] == [
            "    import json",
            "    print(1)",
            "  └ 1",
            "    + ещё 2 строки",
        ]
        row = _frame_row(pilot, "import json")
        assert _colour(row, "import json") == SUB_INK.lower()


@pytest.mark.asyncio
async def test_ran_multiline_command_and_long_output(tmp_path):
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 60)) as pilot:
        await _open(pilot)
        await _tail(
            pilot,
            thread,
            _run("x1", _SCRIPT),
            _out("x1", _envelope(_lines(14))),
        )
        lines = _rows(pilot)
        assert lines[-1] == "    + ещё 13 строк"
        assert sum("+ ещё" in line for line in lines) == 1
        await _toggle(pilot)
        lines = _rows(pilot)
        assert "    x = 2" in lines and "    EOF" in lines
        assert "    line 13" in lines
        assert lines.index("    EOF") < lines.index("  └ line 0")
        assert lines[-1] == "    − свернуть"


@pytest.mark.asyncio
async def test_running_multiline_expand_survives_done(tmp_path):
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        await _open(pilot)
        await _tail(pilot, thread, _run("x1", _SCRIPT))
        lines = _rows(pilot)
        assert lines[1].startswith("● Running  python - <<'EOF'")
        assert lines[-1] == "    + ещё 2 строки"
        await _toggle(pilot)
        assert "    EOF" in _rows(pilot)
        await _tail(pilot, thread, _out("x1", _envelope("1\n")))
        lines = _rows(pilot)
        assert lines[1].startswith("● Ran  python - <<'EOF'")
        assert "    EOF" in lines
        assert lines[-1] == "    − свернуть"


@pytest.mark.asyncio
async def test_single_line_command_as_before(tmp_path):
    """Однострочная команда — одна строка, без продолжения и фраз."""
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        await _open(pilot)
        await _tail(
            pilot, thread, _run("x1", "ls"), _out("x1", _envelope("a\n"))
        )
        lines = _rows(pilot)
        assert lines[1].startswith("● Ran  ls")
        assert lines[2:] == ["  └ a"]
