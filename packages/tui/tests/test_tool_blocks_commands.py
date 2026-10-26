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
from gc_tui.messages import BlockToggle
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
        # До целой секунды — без таймера.
        assert "● Running  uv run pytest -q" in lines
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
        # Свёрнутый вывод — хвост; фраза — над ним, где вывод начинается
        # (решение 2026-10-26).
        assert lines[2:] == [
            "  └ … ещё 11 строк",
            "    line 11",
            "    line 12",
            "    line 13",
        ]
        row = _frame_row(pilot, "pytest")
        assert _colour(row, "pytest") == SUB_INK.lower()
        assert _colour(_frame_row(pilot, "line 12"), "line 12") == DIM.lower()
        await _toggle(pilot)
        lines = _rows(pilot)
        assert lines[2:4] == ["  └ line 0", "    line 1"]
        assert lines[-2:] == ["    line 13", "    − свернуть"]
        assert len(lines) == 2 + 14 + 1


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
@pytest.mark.parametrize(
    "n,tail",
    [
        # 4 строки — видны все: одну строку фраза не прячет.
        (4, ["    err 0", "    err 1", "    err 2", "    err 3"]),
        (5, ["    … ещё 2 строки", "    err 2", "    err 3", "    err 4"]),
    ],
)
async def test_ran_failed_exit_code(tmp_path, n, tail):
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        await _open(pilot)
        out = _envelope("", _lines(n, "err"), exit_code=2)
        await _tail(pilot, thread, _run("x1", "make docs"), _out("x1", out))
        lines = _rows(pilot)
        assert lines[1].startswith("● Ran  make docs")
        assert _colour(_frame_row(pilot, "Ran  make"), "●") == ERR.lower()
        assert lines[2:] == ["  └ код выхода 2", *tail]
        assert _colour(_frame_row(pilot, "код выхода"), "код") == ERR.lower()
        assert not any("· код выхода" in line for line in lines)


@pytest.mark.asyncio
async def test_ran_failed_one_reason(tmp_path):
    """Упавшая команда: причина — одна строка `код выхода N`, общий
    текст ошибки тула её не повторяет (§4)."""
    thread = _Thread(tmp_path)
    out = json.dumps(
        {
            "tool": "exec_python",
            "ok": False,
            "return_code": 1,
            "timed_out": False,
            "error": {
                "type": "non_zero_exit",
                "message": "Process exited with status 1",
            },
            "stdout": _channel(""),
            "stderr": _channel("ls: no such dir\n"),
        }
    )
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        await _open(pilot)
        call = _run("x1", "ls /nope", name="exec_python")
        await _tail(pilot, thread, call, _out("x1", out, "exec_python"))
        lines = _rows(pilot)
        assert lines[2:] == ["  └ код выхода 1", "    ls: no such dir"]


@pytest.mark.asyncio
async def test_ran_error_without_exit_code_keeps_reason(tmp_path):
    """Ошибка до исполнения (кода выхода нет) — причина остаётся."""
    thread = _Thread(tmp_path)
    out = json.dumps(
        {
            "tool": "exec_command",
            "ok": False,
            "error": {"type": "invalid_args", "message": "bad cwd"},
        }
    )
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        await _open(pilot)
        await _tail(pilot, thread, _run("x1", "ls"), _out("x1", out))
        lines = _rows(pilot)
        assert len(lines[2:]) == 1
        assert lines[2].startswith("  └ ")
        assert "bad cwd" in lines[2]


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
        assert lines[2:] == [
            "  └ … ещё 3 строки",
            "    h3",
            "    t0",
            "    t1",
        ]
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
async def test_ran_phrases_under_owners(tmp_path):
    """Фраза — под тем, что раскрывает (решение 2026-10-26): фраза вывода
    — под выводом, над файлами; файлы, показанные целиком, — без фраз."""
    thread = _Thread(tmp_path)
    _journal(
        tmp_path,
        _shell_record("a.py", n=2),
        _shell_record("new.py", "create", n=10),
        _shell_record("b.py", n=2),
        _shell_record("c.py", n=2),
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
        assert lines[2:6] == [
            "  └ … ещё 7 строк",
            "    line 7",
            "    line 8",
            "    line 9",
        ]
        assert lines[6] == "    … ещё 2"
        assert any(
            line.startswith("    Added new.py (+10 −0)") for line in lines
        )
        assert "    Deleted gone.py" in lines
        # Видимые файлы влезают в превью целиком — своих фраз нет; a.py и
        # b.py скрыты окном.
        assert not any("+ ещё" in line for line in lines)
        trace = scr.query_one("#trace", VerticalScroll)
        files = [
            r
            for r in trace.query(ToolCallRow)
            if r.call_id.startswith("x1:") and r.display
        ]
        # По пути: a.py, b.py (скрыты), c.py, gone.py, new.py; после
        # `Deleted` тела нет — new.py без пустой строки.
        assert [r.call_id for r in files] == [
            "x1:c.py",
            "x1:gone.py",
            "x1:new.py",
        ]
        assert [r.styles.margin.top for r in files] == [0, 1, 0]
        diffs = [d for d in trace.query(DiffBlock) if d.display]
        assert len(diffs) == 2


def _gone(path):
    return _shell_record(path, "delete", hunks="", flags=["no_diff"])


def _block_tail(pilot) -> list[str]:
    """Строки кадра от вывода `└` до последней непустой, пустые внутри
    — тоже."""
    rows = [r.plain[ORIGIN:].rstrip() for r in _frame(pilot)]
    start = next(i for i, r in enumerate(rows) if r.startswith("  └"))
    end = max(i for i, r in enumerate(rows) if r)
    return rows[start : end + 1]


@pytest.mark.asyncio
async def test_ran_deleted_files_follow_without_gap(tmp_path):
    """Файлы без тела (`Deleted`) — подряд, без пустых строк (§4.2)."""
    thread = _Thread(tmp_path)
    _journal(tmp_path, _gone("p4a.txt"), _gone("p4b.txt"), _gone("p4c.txt"))
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        await _open(pilot)
        await _tail(pilot, thread, _run("x1", "rm"), _out("x1", _envelope()))
        assert _block_tail(pilot) == [
            "  └ (нет вывода)",
            "    Deleted p4a.txt",
            "    Deleted p4b.txt",
            "    Deleted p4c.txt",
        ]


@pytest.mark.asyncio
async def test_ran_gap_only_after_diff_file(tmp_path):
    """Дифф, пустая строка, затем файлы без тела подряд."""
    thread = _Thread(tmp_path)
    _journal(
        tmp_path, _shell_record("a.py", n=1), _gone("b.txt"), _gone("c.txt")
    )
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        await _open(pilot)
        await _tail(pilot, thread, _run("x1", "make"), _out("x1", _envelope()))
        tail = _block_tail(pilot)
        assert tail[1].startswith("    Edited a.py (+1 −0)")
        assert tail[2:] == [
            "      1 +row 0",
            "",
            "    Deleted b.txt",
            "    Deleted c.txt",
        ]


@pytest.mark.asyncio
async def test_ran_file_preview_budget_changes_then_context(tmp_path):
    """Файл под `Ran` — правило превью то же (решение 2026-10-26): 6
    изменённых и 6 контекста при бюджете 10 — изменённые и 4 ближайших
    строки контекста, фраза — 2 скрытые; раскрытие — дифф целиком."""
    thread = _Thread(tmp_path)
    hunks = (
        "@@ -1,9 +1,9 @@\n a\n b\n c\n-o1\n-o2\n-o3\n+n1\n+n2\n+n3\n"
        " e\n f\n g\n"
    )
    _journal(tmp_path, _shell_record("a.py", hunks=hunks))
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        await _open(pilot)
        await _tail(pilot, thread, _run("x1", "make"), _out("x1", ""))
        lines = _rows(pilot)
        assert lines[3:] == [
            "      2  b",
            "      3  c",
            "      4 -o1",
            "      5 -o2",
            "      6 -o3",
            "      4 +n1",
            "      5 +n2",
            "      6 +n3",
            "      7  e",
            "      8  f",
            "    + ещё 2 строки",
        ]
        await _toggle(pilot)
        assert "      1  a" in _rows(pilot)


@pytest.mark.asyncio
async def test_ran_files_unrecorded_fit_window(tmp_path):
    """Файл и 3 незаписанных — 4 элемента окна: видно всё, фразы нет."""
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
        assert "    ещё 3 файла не записано" in lines
        assert not any("… ещё" in line for line in lines)


@pytest.mark.asyncio
async def test_ran_files_unrecorded(tmp_path):
    thread = _Thread(tmp_path)
    _journal(tmp_path, _shell_record("a.py", n=1, paths=5))
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        await _open(pilot)
        await _tail(
            pilot,
            thread,
            _run("x1", "codegen"),
            _out("x1", _envelope("ok\n")),
        )
        lines = _rows(pilot)
        assert "    … ещё 4" in lines
        await _toggle(pilot)
        lines = _rows(pilot)
        assert "    ещё 4 файла не записано" in lines


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
        # Фраза команды — под её продолжением, где скрытые строки.
        assert lines[2:] == [
            "    import json",
            "    print(1)",
            "    + ещё 2 строки",
            "  └ 1",
        ]
        row = _frame_row(pilot, "import json")
        assert _colour(row, "import json") == SUB_INK.lower()


@pytest.mark.asyncio
async def test_ran_four_line_command_all_shown(tmp_path):
    """Команда в 4 строки — видна целиком: одну строку фраза не прячет."""
    thread = _Thread(tmp_path)
    script = "python - <<'EOF'\nimport json\nprint(1)\nEOF"
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        await _open(pilot)
        await _tail(
            pilot, thread, _run("x1", script), _out("x1", _envelope("1\n"))
        )
        lines = _rows(pilot)
        assert lines[2:] == [
            "    import json",
            "    print(1)",
            "    EOF",
            "  └ 1",
        ]
        await _toggle(pilot)
        assert _rows(pilot)[2:] == lines[2:]


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
        assert lines[2:] == [
            "    import json",
            "    print(1)",
            "    + ещё 2 строки",
            "  └ … ещё 11 строк",
            "    line 11",
            "    line 12",
            "    line 13",
        ]
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


@pytest.mark.asyncio
async def test_ran_short_output_whole_without_phrase(tmp_path):
    """Вывод в превью влезает — показан целиком, фраз нет."""
    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        await _open(pilot)
        await _tail(
            pilot, thread, _run("x1", "ls"), _out("x1", _envelope(_lines(3)))
        )
        assert _rows(pilot)[2:] == ["  └ line 0", "    line 1", "    line 2"]


@pytest.mark.asyncio
async def test_ran_click_top_phrase_expands_collapse_at_bottom(tmp_path):
    """Клик по `… ещё N строк` над хвостом раскрывает весь вывод, клик по
    `− свернуть` под ним — сворачивает к хвосту."""
    from gc_tui.widgets.tool_blocks_view import BlockAction

    thread = _Thread(tmp_path)
    async with _app(tmp_path).run_test(size=(100, 40)) as pilot:
        scr = await _open(pilot)
        out = _envelope(_lines(6), exit_code=1)
        await _tail(pilot, thread, _run("x1", "make"), _out("x1", out))
        phrase = next(w for w in scr.query(".ran-phrase") if w.display)
        assert "… ещё 3 строки" in str(phrase.render())
        await pilot.click(phrase)
        await pilot.pause()
        lines = _rows(pilot)
        assert lines[2:5] == ["  └ код выхода 1", "    line 0", "    line 1"]
        assert lines[-1] == "    − свернуть"
        collapse = next(
            w
            for w in scr.query(BlockAction)
            if w.display and w.phrase == "− свернуть"
        )
        await pilot.click(collapse)
        await pilot.pause()
        assert _rows(pilot)[2:] == [
            "  └ код выхода 1",
            "    … ещё 3 строки",
            "    line 3",
            "    line 4",
            "    line 5",
        ]
