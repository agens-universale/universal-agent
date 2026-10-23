"""Блоки вызовов P4 одним кадром (макеты `p4-21-turn`, `p4-18-agent`,
`p4-10-ran-files`, многострочная команда; план Task 20).

Тред пишется настоящими функциями `gc_core.thread_store` и открывается
возвратом в сессию: время вызовов — из записи итога хода (`outcome.calls`),
правки — из журнала правок, вложенные события — из sidecar субагента.
Часы — общие часы эталонов (`pinned_snapshot_clock`); каждый кадр до
записи проходит сетку колонок `assert_trace_grid`.
"""

from __future__ import annotations

import json
from datetime import UTC, timedelta

import pytest
from gc_testkit.tui_support import SNAPSHOT_NOW, tui_settings

pytest.importorskip("textual")
pytest.importorskip("pytest_textual_snapshot")

from gc_core.agent_edits.journal import EditRecord, EditsJournal
from gc_core.persistence.turn_outcome import TurnOutcome
from gc_core.thread_store import (
    append_new_thread_messages,
    append_subagent_trace_record,
    append_turn_outcome_record,
    edits_journal_path,
)

_SYS = "cli"
_TID = "7f3a9b1c"
_SIZE = (100, 40)
_ORIGIN = 1
_ASK = "почему клиент не ретраит 503? почини и проверь тестами"
_PYTEST = "uv run pytest tests/test_client.py -q"
_FAILED = "FAILED tests/test_client.py::test_retry_503 - assert 1 == 3"


# --- записи треда -----------------------------------------------------------


def _utc_iso(moment) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _call(call_id: str, name: str, args: dict) -> dict:
    return {
        "id": f"fc_{call_id}",
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(args, ensure_ascii=False),
    }


def _out(call_id: str, name: str, output: str) -> dict:
    return {
        "id": f"fco_{call_id}",
        "type": "function_call_output",
        "call_id": call_id,
        "name": name,
        "output": output,
    }


def _ok(name: str, **facts) -> str:
    return json.dumps({"tool": name, "ok": True, **facts}, ensure_ascii=False)


def _envelope(stdout: str, exit_code: int = 0) -> str:
    """Конверт `exec_command`, как пишет ядро."""
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


def _say(mid: str, text: str) -> dict:
    return {"id": mid, "role": "assistant", "content": text}


def _read(cid: str, path: str, lines: int) -> list[dict]:
    name = "fs_read_file"
    return [
        _call(cid, name, {"path": path}),
        _out(cid, name, _ok(name, total_lines=lines)),
    ]


def _search(cid: str, pattern: str, path: str, matches: int) -> list[dict]:
    name = "fs_find_in_files"
    return [
        _call(cid, name, {"pattern": pattern, "path": path}),
        _out(cid, name, _ok(name, matches=matches)),
    ]


def _find(cid: str, query: str, entries: list[str]) -> list[dict]:
    name = "fs_find_files_by_name"
    return [
        _call(cid, name, {"query": query}),
        _out(cid, name, _ok(name, entries=entries, count=len(entries))),
    ]


def _run(cid: str, command: str, stdout: str, code: int = 0) -> list[dict]:
    name = "exec_command"
    return [
        _call(cid, name, {"command": command}),
        _out(cid, name, _envelope(stdout, code)),
    ]


def _edit(cid: str, path: str) -> list[dict]:
    name = "fs_apply_file_edits"
    return [_call(cid, name, {"path": path}), _out(cid, name, _ok(name))]


def _write_thread(tmp_path, payloads: list[dict], calls: dict, edits) -> None:
    """Тред одного хода: реплика, записи, запись итога со временем вызовов;
    свежая строка итога — со сводкой правок хода (`edits`)."""
    meta = {
        "thread_id": _TID,
        "system_id": _SYS,
        "tui": {"status": "completed", "last_indicator": {"edits": edits}},
    }
    (tmp_path / f"meta_{_SYS}_{_TID}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    messages = [{"id": "u1", "role": "user", "content": _ASK}, *payloads]
    append_new_thread_messages(str(tmp_path), _SYS, _TID, messages, set())
    ended = SNAPSHOT_NOW - timedelta(minutes=8)
    outcome = TurnOutcome(
        status="done",
        reason=None,
        opened_by="u1",
        started_at=_utc_iso(ended - timedelta(seconds=134)),
        ended_at=_utc_iso(ended),
        duration_sec=134.0,
        error=None,
        calls=calls,
    )
    append_turn_outcome_record(
        str(tmp_path), _SYS, _TID, outcome, seq=len(messages)
    )


def _hunks(groups: list[list[tuple[str, int, str]]]) -> str:
    """Ханки журнала из строк макета `(знак, номер, текст)`: группа — один
    ханк, номера в кадре те же, что в макете."""
    out = []
    for rows in groups:
        old = [no for sign, no, _ in rows if sign == "-"]
        new = [no for sign, no, _ in rows if sign == "+"]
        head = f"@@ -{old[0] if old else 0},{len(old)}"
        head += f" +{new[0] if new else 0},{len(new)} @@\n"
        out.append(head + "".join(f"{sign}{t}\n" for sign, _, t in rows))
    return "".join(out)


def _journal(tmp_path, records: list[EditRecord]) -> None:
    journal = EditsJournal(edits_journal_path(str(tmp_path), _SYS, _TID))
    for record in records:
        journal.append(record)


def _record(path, groups, *, add, rem, call_id=None, op="edit", command=None):
    """Запись журнала; удаление — как пишет ядро: без ханков, `no_diff`."""
    return EditRecord(
        path=path,
        turn_id="t1",
        ts=1.0,
        call_id=call_id,
        op=op,
        hunks=_hunks(groups),
        add=add,
        del_=rem,
        before_sha256=None,
        after_sha256=None,
        flags=["no_diff"] if op == "delete" else [],
        command_call_id=command,
    )


# --- p4-21-turn --------------------------------------------------------------

_DIFF_HTTP = [
    [
        ("-", 2, "from client.transport import send"),
        ("+", 2, "from client.retry import with_retry"),
        ("+", 3, "from client.retry_policy import RETRY_ON"),
    ],
    [
        ("-", 42, "        resp = send(req)"),
        ("+", 43, "        resp = with_retry(send, req,"),
        ("+", 44, "                          on=RETRY_ON)"),
    ],
]
_DIFF_CFG = [[("-", 12, "TIMEOUT_SEC = 10"), ("+", 12, "TIMEOUT_SEC = 30")]]
_DIFF_TEST = [
    [
        ("-", 18, "def test_no_retry_on_503(client):"),
        ("+", 18, "def test_retry_on_503(client):"),
        ("-", 19, "    client.fake(503)"),
        ("+", 19, "    client.fake(503, 503, 200)"),
        *[
            ("+", 20 + i, f"    assert client.calls[{i}] == 503")
            for i in range(5)
        ],
    ]
]


def _turn(tmp_path) -> None:
    payloads = [
        _say("a1", "Посмотрю, как клиент делает запросы и где живут ретраи."),
        *_read("r0", "client/http.py", 120),
        *_read("r1", "client/retry.py", 64),
        *_search("r2", "503", "client/", 1),
        *_read("r3", "client/config.py", 40),
        *_find("r4", "tests/*client*", ["a", "b"]),
        *_search("r5", "RETRY_ON", "packages/", 3),
        *_read("r6", "tests/test_client.py", 88),
        *_run("x1", _PYTEST, f"{_FAILED}\n", code=1),
        _say("a2", "Код 503 не в списке повторяемых. Поправлю клиент, "
             "конфиг и тест."),
        *_edit("e0", "client/http.py"),
        *_edit("e1", "client/config.py"),
        *_edit("e2", "tests/test_client.py"),
        *_run("x2", _PYTEST, "..............  [100%]\n14 passed in 0.66s\n"),
        _say("a3", "Готово: 503 и 504 теперь повторяются, до 5 попыток; "
             "таймаут 30 с."),
    ]  # fmt: skip
    calls = {f"r{i}": 1.0 for i in range(7)}
    calls.update(r2=2.0, r5=3.0, x1=5.0, x2=4.0, e0=1.0, e1=1.0, e2=1.0)
    _write_thread(tmp_path, payloads, calls, edits=[3, 12, 5])
    _journal(
        tmp_path,
        [
            _record("client/http.py", _DIFF_HTTP, add=4, rem=2, call_id="e0"),
            _record("client/config.py", _DIFF_CFG, add=1, rem=1, call_id="e1"),
            _record(
                "tests/test_client.py", _DIFF_TEST, add=7, rem=2, call_id="e2"
            ),
        ],
    )


# --- p4-18-agent -------------------------------------------------------------

_AGENT = "call_agent_1"


def _sidecar(tmp_path, kind: str, payload: dict) -> None:
    append_subagent_trace_record(
        str(tmp_path),
        _SYS,
        _TID,
        parent_call_id=_AGENT,
        child_run_id="c1",
        kind=kind,
        payload=payload,
    )


def _nested_call(tmp_path, cid: str, name: str, args: dict, output: str):
    call = {
        "type": "function_call",
        "name": name,
        "arguments": json.dumps(args, ensure_ascii=False),
        "call_id": cid,
    }
    end = {"type": "function_call_output", "call_id": cid, "output": output}
    _sidecar(tmp_path, "call", call)
    _sidecar(tmp_path, "result", end)


def _agent(tmp_path) -> None:
    name = "subagent_worker"
    payloads = [
        _say("a1", "Поручу проверку клиента субагенту."),
        _call(_AGENT, name, {"task": "проверка клиента"}),
        _out(_AGENT, name, json.dumps({"ok": True, "result": "готово"})),
        _say("a2", "Субагент подтвердил: таймаут и ретраи работают, "
             "42 теста проходят."),
    ]  # fmt: skip
    calls = {_AGENT: 72.0, "n0": 1.0, "n1": 1.0, "n2": 1.0, "n3": 1.0}
    calls["n4"] = 11.0
    _write_thread(tmp_path, payloads, calls, edits=None)
    _sidecar(
        tmp_path,
        "intermediate_reply",
        {"prose": "Смотрю, где клиент берёт таймаут и коды ретраев."},
    )
    read, search = "fs_read_file", "fs_find_in_files"
    for cid, path, lines in (
        ("n0", "client/http.py", 120),
        ("n1", "client/config.py", 40),
    ):
        _nested_call(
            tmp_path, cid, read, {"path": path}, _ok(read, total_lines=lines)
        )
    _nested_call(
        tmp_path,
        "n2",
        search,
        {"pattern": "timeout", "path": "client/"},
        _ok(search, matches=4),
    )
    _nested_call(
        tmp_path,
        "n3",
        read,
        {"path": "client/retry_policy.py"},
        _ok(read, total_lines=12),
    )
    _nested_call(
        tmp_path,
        "n4",
        "exec_command",
        {"command": "uv run pytest tests -q"},
        _envelope("42 passed in 3.10s\n"),
    )
    _sidecar(
        tmp_path,
        "intermediate_reply",
        {"prose": "Клиент читает таймаут из конфига, тесты проходят."},
    )
    _sidecar(tmp_path, "finished", {"ok": True, "finish_reason": "completed"})


# --- p4-10-ran-files ---------------------------------------------------------

_GEN = "python scripts/gen_retry_table.py && git rm -q client/legacy_retry.py"
_TABLE = [[
    ("+", 1, '"""Сгенерировано scripts/gen_retry_table.py."""'),
    ("+", 2, ""),
    ("+", 3, "TABLE = {"),
    *[("+", 4 + i, f"    {c}: ({d}, 5),") for i, (c, d) in enumerate(
        ((429, 0.5), (500, 1.0), (502, 1.0), (503, 2.0), (504, 2.0)))],
    ("+", 9, "}"),
]]  # fmt: skip
_INIT = [[("+", 3, "from client.retry_table import TABLE")]]


def _ran_files(tmp_path) -> None:
    payloads = [
        _say("a1", "Сгенерирую таблицу задержек и уберу старый модуль."),
        *_run("x1", _GEN, "записано client/retry_table.py: 5 кодов\n"),
    ]
    _write_thread(tmp_path, payloads, {"x1": 2.0}, edits=[3, 10, 4])
    _journal(
        tmp_path,
        [
            _record(
                "client/retry_table.py",
                _TABLE,
                add=9,
                rem=0,
                op="create",
                command="x1",
            ),
            _record("client/__init__.py", _INIT, add=1, rem=0, command="x1"),
            _record(
                "client/legacy_retry.py",
                [],
                add=0,
                rem=0,
                op="delete",
                command="x1",
            ),
        ],
    )


# --- многострочная команда --------------------------------------------------

_HEREDOC = """python - <<'EOF'
from client.retry_policy import RETRY_ON, MAX_ATTEMPTS
from client.retry_table import TABLE
for code in sorted(RETRY_ON):
    delay, attempts = TABLE[code]
    print(code, delay, min(attempts, MAX_ATTEMPTS))
EOF"""
_CODES = ((408, 0.5), (425, 0.5), (429, 0.5), (500, 1.0), (502, 1.0),
          (503, 2.0), (504, 2.0), (507, 2.0), (509, 4.0), (520, 4.0),
          (522, 4.0))  # fmt: skip


def _multiline(tmp_path) -> None:
    out = "code delay attempts\n" + "".join(f"{c} {d} 5\n" for c, d in _CODES)
    payloads = [
        _say("a1", "Сверю таблицу задержек с политикой ретраев."),
        *_run("x1", _HEREDOC, out),
    ]
    _write_thread(tmp_path, payloads, {"x1": 1.0}, edits=None)


# --- кадр --------------------------------------------------------------------


def _app(tmp_path):
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path),
        system=_SYS,
        settings=tui_settings(
            {
                "agent.model": "CodeAgent",
                "backend.api_mode": "chat_completions",
            }
        ),
    )


def _grid_rows(app) -> list:
    """Строки ленты кадра от первой видимой реплики или события (над
    ними — бренд-карточка) без строки итога хода."""
    from gc_testkit.trace_grid import trace_rows

    from gc_tui.widgets.turn_line import TurnLine

    rows = trace_rows(app, exclude=(TurnLine,))
    start = next(
        i
        for i, r in enumerate(rows)
        if r.plain[_ORIGIN : _ORIGIN + 1] in ("›", "●")
    )
    return rows[start:]


def _snap(snap_compare, monkeypatch, tmp_path, write) -> bool:
    from gc_testkit.trace_grid import assert_trace_grid

    from gc_tui.screens.main import TraceScreen
    from gc_tui.services import terminal_caps

    # Папку в шапке и глиф ❐/⧉ (зависит от терминала) фиксируем — иначе
    # снапшот стал бы машинно-зависимым.
    monkeypatch.setattr(
        TraceScreen, "_current_cwd", lambda self: "/work/demo-project"
    )
    monkeypatch.setattr(terminal_caps, "supports_font_fallback", lambda: False)
    write(tmp_path)

    async def setup(pilot):
        await pilot.pause()
        pilot.app.screen.attach_active_task(_TID, is_resume=True)
        # Мигание курсора поля — таймер 0.5 с, фаза в кадре от нагрузки;
        # без мигания курсор виден всегда (приём `test_snapshot_forms`).
        pilot.app.screen.query_one("#cmdinput").cursor_blink = False
        await pilot.pause(0.5)
        assert_trace_grid(_grid_rows(pilot.app), origin=_ORIGIN)

    return snap_compare(_app(tmp_path), terminal_size=_SIZE, run_before=setup)


def test_tool_blocks_turn_snapshot(snap_compare, tmp_path, monkeypatch):
    assert _snap(snap_compare, monkeypatch, tmp_path, _turn)


def test_tool_blocks_subagent_snapshot(snap_compare, tmp_path, monkeypatch):
    assert _snap(snap_compare, monkeypatch, tmp_path, _agent)


def test_tool_blocks_ran_files_snapshot(snap_compare, tmp_path, monkeypatch):
    assert _snap(snap_compare, monkeypatch, tmp_path, _ran_files)


def test_tool_blocks_multiline_command_snapshot(
    snap_compare, tmp_path, monkeypatch
):
    assert _snap(snap_compare, monkeypatch, tmp_path, _multiline)
